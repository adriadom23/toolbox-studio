"""Update the HUB Delivery RTS workbook with the analyzed returns.

The operator stores the RTS file location in configuration. Two modes
are supported, in this priority order:

1. **Online (SharePoint REST).** When a ``*.sharepoint.com`` URL is
   configured (``HUB_RTS_SHAREPOINT_URL`` or a URL in
   ``HUB_RTS_SHAREPOINT_FILE``) the workbook is downloaded, edited in
   memory and uploaded back **directly to SharePoint**, reusing the
   browser session cookies — no local OneDrive copy required. See
   :mod:`app.integrations.sharepoint_client`.
2. **Local file.** Otherwise a local/UNC path (OneDrive-synced document
   library) is edited in place.

On the Slack-send path a missing/unreachable RTS is **skipped silently**
(never breaks the message); the ``/update-rts`` route uses the diagnosed
variant to surface a precise reason instead.

Behaviour on the workbook (first worksheet unless overridden):
  * Column **D** holds the tracking IDs.
  * For every existing data row, column **J** is set to ``YES`` when its
    column-D tracking ID matches one from the analysis, otherwise ``NO``.
    When a row is marked ``NO`` the next column (**K**) gets the note
    ``"No information from DSP"``; that note is cleared on ``YES`` rows.
  * Only rows ALREADY present in the workbook are touched. Tracking IDs
    from the analysis that are not in the workbook are never appended —
    they are just counted in the ``missing`` field of the summary.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Iterable

from app.integrations.sharepoint_client import (
    SharePointError,
    download_file,
    is_sharepoint_url,
    upload_file,
)


# 1-based column indices used as fallback when headers can't be detected
# (D = 4 tracking IDs, J = 10 the RTS YES/NO flag).
COL_TRACKING = 4
COL_FLAG = 10
YES = "YES"
NO = "NO"
# Text written in the column right after the RTS flag when a tracking ID is
# marked ``NO`` (i.e. it is not among the diagnosed returns from the DSP).
NO_INFO_NOTE = "No information from DSP"

# Header labels (normalized: upper-case, no spaces/underscores) used to
# locate the tracking-ID and RTS columns by name instead of fixed position.
#
# NOTE: a bare ``ID`` is deliberately *not* a tracking header. RTS sheets
# routinely carry an ``ID`` index/sequence column that sits before the real
# ``tracking_id`` column; matching it would lock detection onto the wrong
# column, so no data row ever gets marked and every return is appended as a
# junk row instead. Detection is done by priority (see ``_tracking_rank``).
_TRACKING_HEADERS = {
    "TRACKINGID", "TRACKING", "TBA", "SHIPMENTID",
}
_RTS_HEADERS = {"RTS"}

# Env keys, in priority order, that may hold the RTS location.
_LOCATION_KEYS = (
    "HUB_RTS_SHAREPOINT_FILE",
    "HUB_RTS_SHAREPOINT",
    "HUB_RTS_SHAREPOINT_URL",
)

_HEADER_LABELS = {
    "TRACKING ID", "TRACKINGID", "TRACKING_ID", "TRACKING", "TBA",
    "SHIPMENT ID", "ID", "D",
}


def _resolve_rts_location() -> tuple[str, str]:
    """Resolve the configured RTS location.

    Returns ``(path, reason)`` where ``path`` is the first configured
    local/UNC path (empty if none) and ``reason`` explains an empty
    result: ``"not_configured"`` when no key holds any value, or
    ``"url_only"`` when the only configured value(s) are online
    ``http(s)`` SharePoint links (which we cannot write to directly).
    """
    saw_value = False
    saw_url = False
    for key in _LOCATION_KEYS:
        raw = (os.environ.get(key) or "").strip().strip('"').strip("'")
        if not raw:
            continue
        saw_value = True
        if raw.lower().startswith(("http://", "https://")):
            saw_url = True
            continue
        return raw, "ok"
    if saw_url:
        return "", "url_only"
    if saw_value:
        return "", "not_configured"
    return "", "not_configured"


def _resolve_rts_path() -> str:
    """Backward-compatible helper: first configured local/UNC path."""
    return _resolve_rts_location()[0]


# URL keys, in priority order, that may hold an online SharePoint link.
_URL_KEYS = (
    "HUB_RTS_SHAREPOINT_URL",
    "HUB_RTS_SHAREPOINT_FILE",
    "HUB_RTS_SHAREPOINT",
)


def _resolve_rts_url() -> str:
    """Return the first configured value that is a SharePoint Online URL."""
    for key in _URL_KEYS:
        raw = (os.environ.get(key) or "").strip().strip('"').strip("'")
        if raw and is_sharepoint_url(raw):
            return raw
    return ""


def _build_wanted(tracking_ids: Iterable[str]) -> dict[str, str]:
    """Return ``{UPPER_tid: original_tid}`` preserving original casing."""
    wanted_map: dict[str, str] = {}
    for raw in tracking_ids or []:
        token = str(raw or "").strip()
        if not token:
            continue
        up = token.upper()
        if up not in wanted_map:
            wanted_map[up] = token
    return wanted_map


def _mutate_worksheet(worksheet, wanted_map: dict[str, str]) -> dict:
    """Apply the YES/NO marking to ``worksheet`` in place.

    Only reviews rows that already exist in the workbook — tracking IDs
    from the analysis that are not present are NEVER appended, just
    counted in ``missing``. Returns a summary dict (``marked_yes`` /
    ``marked_no`` / ``missing`` / ``sheet``).
    """
    wanted = set(wanted_map)
    tracking_col, flag_col, header_row = _detect_columns(worksheet)

    seen: set[str] = set()
    marked_yes = 0
    marked_no = 0
    last_row = worksheet.max_row or 0

    for row in range(1, last_row + 1):
        if row <= header_row:
            continue
        tid = str(worksheet.cell(row=row, column=tracking_col).value or "").strip()
        if not tid:
            continue
        up = tid.upper()
        # Skip an obvious header that slipped past detection.
        if up in _HEADER_LABELS or not _looks_like_tracking(up):
            continue
        if up in wanted:
            worksheet.cell(row=row, column=flag_col).value = YES
            # Clear any stale "No information from DSP" note next to a YES.
            worksheet.cell(row=row, column=flag_col + 1).value = None
            seen.add(up)
            marked_yes += 1
        else:
            worksheet.cell(row=row, column=flag_col).value = NO
            worksheet.cell(row=row, column=flag_col + 1).value = NO_INFO_NOTE
            marked_no += 1

    # Devoluciones del análisis que no existen en el libro: NO se añaden
    # (solo se revisan las filas ya presentes); se cuentan a título
    # informativo.
    missing = [wanted_map[up] for up in wanted_map if up not in seen]

    return {
        "sheet": worksheet.title,
        "marked_yes": marked_yes,
        "marked_no": marked_no,
        "missing": len(missing),
    }


def _pick_worksheet(workbook, sheet: str | None):
    sheet_name = (sheet or os.environ.get("HUB_RTS_SHAREPOINT_SHEET") or "").strip()
    if sheet_name and sheet_name in workbook.sheetnames:
        return workbook[sheet_name]
    return workbook.active


def _update_rts_online(
    url: str, wanted_map: dict[str, str], *, sheet: str | None
) -> tuple[dict | None, str]:
    """Download → edit → upload the RTS workbook over the SharePoint API."""
    from openpyxl import load_workbook

    try:
        content, target, session = download_file(url)
    except SharePointError as exc:
        return None, f"online_{exc.reason}:{exc}"

    keep_vba = target.filename.lower().endswith(".xlsm")
    try:
        workbook = load_workbook(io.BytesIO(content), keep_vba=keep_vba)
    except Exception as exc:  # noqa: BLE001
        return None, f"load_failed:{exc}"

    try:
        worksheet = _pick_worksheet(workbook, sheet)
        summary = _mutate_worksheet(worksheet, wanted_map)
        buffer = io.BytesIO()
        workbook.save(buffer)
    finally:
        workbook.close()

    try:
        upload_file(target, buffer.getvalue(), session)
    except SharePointError as exc:
        return None, f"online_{exc.reason}:{exc}"

    summary["file"] = f"https://{target.host}{target.server_relative}"
    summary["mode"] = "online"
    return summary, "ok"


def _looks_like_tracking(value: str) -> bool:
    up = str(value or "").strip().upper()
    return len(up) >= 8 and any(ch.isdigit() for ch in up)


def _norm_header(value: object) -> str:
    return (
        str(value or "")
        .strip()
        .upper()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


def _tracking_rank(header: str) -> int:
    """Return how strongly ``header`` (normalized) names a tracking column.

    Lower is better; ``99`` means "not a tracking column". Ranking lets a
    row that contains both an ``ID`` index column and a real
    ``tracking_id`` column resolve to the latter regardless of order.
    """
    if header == "TRACKINGID":
        return 0
    if "TRACKING" in header:
        return 1
    if header in _TRACKING_HEADERS:  # TBA / SHIPMENTID
        return 2
    return 99


def _detect_columns(worksheet) -> tuple[int, int, int]:
    """Locate the tracking-ID and RTS columns by header name.

    Scans the first few rows for a header row containing a tracking-ID
    column and (optionally) an ``RTS`` column. Within a row the *best*
    tracking candidate wins (see ``_tracking_rank``) so a generic ``ID``
    index column never shadows the real ``tracking_id`` column. Returns
    ``(tracking_col, flag_col, header_row)`` (1-based). Falls back to the
    fixed positions (D/J) with ``header_row = 1`` when nothing matches.
    """
    max_col = worksheet.max_column or 0
    scan_rows = min(worksheet.max_row or 0, 5)
    for row in range(1, scan_rows + 1):
        tracking_col = 0
        tracking_rank = 99
        flag_col = 0
        for col in range(1, max_col + 1):
            header = _norm_header(worksheet.cell(row=row, column=col).value)
            if not header:
                continue
            rank = _tracking_rank(header)
            if rank < tracking_rank:
                tracking_rank = rank
                tracking_col = col
            if not flag_col and header in _RTS_HEADERS:
                flag_col = col
        if tracking_col:
            # RTS column defaults to the fixed flag position when absent.
            return tracking_col, (flag_col or COL_FLAG), row
    return COL_TRACKING, COL_FLAG, 1


def update_rts_returns(
    tracking_ids: Iterable[str],
    *,
    path: str | None = None,
    sheet: str | None = None,
    allow_empty: bool = False,
) -> dict | None:
    """Update the RTS workbook in place (silent variant).

    Returns a summary dict on success, or ``None`` when the update was
    skipped (not configured, file missing/unreadable, or no tracking
    IDs). This function never raises — it is used on the Slack-send path
    where a missing RTS must not break the message.
    """
    result, _reason = update_rts_returns_diagnosed(
        tracking_ids, path=path, sheet=sheet, allow_empty=allow_empty
    )
    return result


def update_rts_returns_diagnosed(
    tracking_ids: Iterable[str],
    *,
    path: str | None = None,
    sheet: str | None = None,
    allow_empty: bool = False,
) -> tuple[dict | None, str]:
    """Update the RTS workbook, returning ``(summary, reason)``.

    ``summary`` is the success dict (or ``None`` on skip/error) and
    ``reason`` is a machine-readable code the caller can turn into a
    user-facing message:

      * ``"ok"`` — written successfully (``summary`` is the dict).
      * ``"no_tids"`` — nothing to write.
      * ``"not_configured"`` — no RTS location in configuration.
      * ``"url_only"`` — only a SharePoint web URL configured (can't write).
      * ``"not_found"`` — configured path does not exist locally.
      * ``"load_failed"`` — the workbook could not be opened.
      * ``"locked"`` — the workbook could not be saved (open elsewhere?).
      * ``"no_tracking_col"`` — no tracking-ID column found in the sheet.

    The optional second element of error tuples carries the offending
    path when relevant (``"not_found:<path>"`` etc.) for richer messages.
    This function never raises.
    """
    try:
        wanted_map = _build_wanted(tracking_ids)
        if not wanted_map and not allow_empty:
            return None, "no_tids"
        # ``allow_empty`` covers the "no returns today" case: the workbook
        # must still be adjusted — every existing row gets ``NO`` plus the
        # "No information from DSP" note, and nothing is appended.

        # ── 1. Online SharePoint mode (preferred) ──
        if path and is_sharepoint_url(path):
            return _update_rts_online(path, wanted_map, sheet=sheet)
        if not path:
            online_url = _resolve_rts_url()
            if online_url:
                return _update_rts_online(online_url, wanted_map, sheet=sheet)

        # ── 2. Local / UNC file mode ──
        if path:
            target = path.strip()
        else:
            target, loc_reason = _resolve_rts_location()
            if not target:
                return None, loc_reason  # "not_configured" | "url_only"

        if not target:
            return None, "not_configured"

        file_path = Path(target).expanduser()
        try:
            if not file_path.is_file():
                return None, f"not_found:{file_path}"
        except OSError:
            return None, f"not_found:{file_path}"

        from openpyxl import load_workbook

        keep_vba = file_path.suffix.lower() == ".xlsm"
        try:
            workbook = load_workbook(file_path, keep_vba=keep_vba)
        except Exception as exc:  # noqa: BLE001
            return None, f"load_failed:{exc}"

        try:
            worksheet = _pick_worksheet(workbook, sheet)
            summary = _mutate_worksheet(worksheet, wanted_map)
            workbook.save(file_path)
        except Exception as exc:  # noqa: BLE001
            return None, f"locked:{exc}"
        finally:
            workbook.close()

        summary["file"] = str(file_path)
        summary["mode"] = "local"
        return summary, "ok"
    except Exception as exc:  # noqa: BLE001 — never let RTS update break the flow.
        return None, f"error:{exc}"
