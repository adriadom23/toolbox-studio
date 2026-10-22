"""Shared DS-level digest builder.

Every tool that fans out per-DSP messages (Bolsas, Horarios, Lockers,
Multi-Items, Atlas Pickup, FtdsRts, DnrsReport, ...) now offers a
``scope=STATION`` option that emits a single consolidated message to
the DS Slack channel + DS email instead of fanning out to every DSP.

This module provides the generic builders so every tool route can opt
in by passing a small dict of pre-computed per-DSP totals. Tools keep
their own per-DSP message builders untouched; the DS digest is built
on top of the same totals the per-DSP messages already used.

Public API:

* :func:`build_slack_digest(title, by_dsp, totals_columns, *, locale)`
  → ASCII-table Slack message with the DSP totals + grand total row.
* :func:`build_email_digest(...)` → Amazon-themed HTML using the
  shared ``email_templates`` (KPI grid + per-DSP table + callout).
* :func:`build_excel_digest(...)` → Single Excel with the per-DSP
  rows the operator can forward.
* :func:`resolve_ds_slack` / :func:`resolve_ds_email` — DS-channel
  resolvers that walk ``SLACK_DSP_PERFO → SLACK_DS → COMMON_SLACK``
  and ``EMAIL_DS → COMMON_EMAIL`` respectively.
* :func:`send_station_digest(...)` — convenience that posts the
  Slack message OR sends the email and returns the same
  ``sent/failed/targets`` shape the per-DSP send loop returns.

The data shape is intentionally generic: ``by_dsp`` is a dict
``{dsp_short: {col_key: value, ...}}`` and ``totals_columns`` is a
list of ``(col_key, label)`` tuples that controls table ordering.
"""
from __future__ import annotations

import html as _html_lib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from app.integrations import email_templates as _email_tpl
from app.integrations.messages_i18n import (
    footer_message,
    signoff_html,
    t as _t,
    translate_message as _translate_message,
)


_log = logging.getLogger("toolbox.station_digest")


# ---------------------------------------------------------------------------
# DS contact resolvers (env-driven, mirrors the legacy precedence each
# tool used to hardcode separately).
# ---------------------------------------------------------------------------


def resolve_ds_slack() -> str:
    """Return the first non-empty SLACK_* webhook for the DS channel."""
    for key in ("SLACK_DSP_PERFO", "SLACK_DS", "COMMON_SLACK"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def resolve_ds_email() -> str:
    """Return the first non-empty EMAIL_* address for the DS recipient."""
    for key in ("EMAIL_DS", "COMMON_EMAIL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Slack ASCII helpers (mirrors the legacy compact table style each
# tool already uses for per-DSP messages).
# ---------------------------------------------------------------------------


def _ascii_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    headers = [str(h) for h in headers]
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in str_rows), default=0))
        for i in range(len(headers))
    ]

    def _fmt(row: Sequence[str]) -> str:
        return "| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = [sep, _fmt(headers), sep]
    out.extend(_fmt(r) for r in str_rows)
    out.append(sep)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StationColumn:
    key: str
    label: str
    align: str = "right"  # "left" | "center" | "right"
    aggregate: str = "sum"  # "sum" | "max" | "first"
    formatter: Any = None  # optional callable(value) -> str for slack/email


def _coerce_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        cleaned = str(value).replace(",", ".").replace("%", "").strip()
        if not cleaned:
            return None
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _fmt_value(value: Any, column: StationColumn) -> str:
    if column.formatter is not None:
        try:
            return str(column.formatter(value))
        except Exception:  # noqa: BLE001
            pass
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _aggregate(values: list[Any], column: StationColumn) -> Any:
    if not values:
        return ""
    if column.aggregate == "first":
        return next((v for v in values if v not in (None, "")), "")
    numbers = [n for n in (_coerce_number(v) for v in values) if n is not None]
    if not numbers:
        return ""
    if column.aggregate == "max":
        return max(numbers)
    return sum(numbers)


# ---------------------------------------------------------------------------
# Slack digest
# ---------------------------------------------------------------------------


def build_slack_digest(
    *,
    title: str,
    intro: str,
    columns: Sequence[StationColumn],
    by_dsp: dict[str, dict[str, Any]],
    total_label: str = "TOTAL",
    footer_lines: Sequence[str] = (),
    locale: str | None = None,
) -> str:
    """Build a single DS Slack message with the per-DSP totals table.

    ``by_dsp`` is ``{dsp_short: {column_key: value, ...}}``. Columns
    not present in a row default to empty/zero. The totals row is
    appended at the bottom. The whole message is run through
    ``translate_message`` so the operator's ``locale`` is honored even
    though callers pass Spanish literals for ``title`` / ``intro`` /
    ``footer_lines``.
    """
    if not columns:
        return ""
    headers = ["DSP"] + [c.label for c in columns]
    rows: list[list[str]] = []
    totals_acc: dict[str, list[Any]] = {c.key: [] for c in columns}
    for dsp in sorted(by_dsp.keys()):
        record = by_dsp[dsp] or {}
        line: list[str] = [dsp]
        for col in columns:
            val = record.get(col.key, "")
            line.append(_fmt_value(val, col))
            if val not in (None, ""):
                totals_acc[col.key].append(val)
        rows.append(line)
    if rows:
        total_row = [total_label]
        for col in columns:
            agg = _aggregate(totals_acc[col.key], col)
            total_row.append(_fmt_value(agg, col))
        rows.append(total_row)
    parts: list[str] = [title]
    if intro:
        parts.append(intro)
    parts.append("```")
    parts.append(_ascii_table(headers, rows) if rows else "(sin datos)")
    parts.append("```")
    parts.extend(line for line in footer_lines if line)
    return _translate_message("\n".join(parts), locale)


# ---------------------------------------------------------------------------
# Email digest
# ---------------------------------------------------------------------------


def _safe(text: Any) -> str:
    return _html_lib.escape(str(text or ""), quote=True)


def build_email_digest(
    *,
    subject: str,
    title: str,
    eyebrow: str,
    intro_html: str,
    kpi_items: Sequence[tuple[str, Any]],
    columns: Sequence[StationColumn],
    by_dsp: dict[str, dict[str, Any]],
    detail_section_title: str,
    callout: str = "",
    callout_tone: str = "info",
    station_label: str = "DS",
    locale: str | None = None,
    total_label: str = "TOTAL",
) -> tuple[str, str]:
    """Render the DS email body. Returns ``(subject, html)``."""
    headers = ["DSP"] + [c.label for c in columns]
    align = ["left"] + [c.align for c in columns]
    table_rows: list[list[Any]] = []
    totals_acc: dict[str, list[Any]] = {c.key: [] for c in columns}
    for dsp in sorted(by_dsp.keys()):
        record = by_dsp[dsp] or {}
        line: list[Any] = [dsp]
        for col in columns:
            val = record.get(col.key, "")
            line.append(_fmt_value(val, col))
            if val not in (None, ""):
                totals_acc[col.key].append(val)
        table_rows.append(line)
    if table_rows:
        total_row = [f"<b>{_safe(total_label)}</b>"]
        for col in columns:
            agg = _aggregate(totals_acc[col.key], col)
            total_row.append(f"<b>{_safe(_fmt_value(agg, col))}</b>")
        table_rows.append(total_row)

    sections = [
        _email_tpl.section(
            _t("common", "section.summary", locale),
            _email_tpl.kpi_grid(list(kpi_items)),
            eyebrow=f"DS {station_label}" if station_label else "DS",
        ),
        _email_tpl.section(
            detail_section_title,
            _email_tpl.styled_table(headers, table_rows, align=align),
        ),
    ]
    if callout:
        sections.append(_email_tpl.callout(callout, tone=callout_tone))

    html = _email_tpl.render_email(
        title=title,
        eyebrow=eyebrow,
        intro_html=intro_html,
        sections=sections,
        signoff_html=signoff_html(locale, _safe(station_label)),
        footer_text=footer_message(locale),
    )
    # Single translation pass over the fully-rendered body so the
    # operator's locale is honored for every hardcoded-Spanish input
    # (title, intro, KPI labels, column headers, callout, ...).
    return _translate_message(subject, locale), _translate_message(html, locale)


# ---------------------------------------------------------------------------
# Excel digest
# ---------------------------------------------------------------------------


def build_excel_digest(
    *,
    output_dir: Path,
    file_stem: str,
    columns: Sequence[StationColumn],
    by_dsp: dict[str, dict[str, Any]],
    extra_rows_per_dsp: dict[str, list[dict[str, Any]]] | None = None,
) -> Path | None:
    """Build a single Excel with per-DSP totals (and optional detail rows).

    ``extra_rows_per_dsp`` lets a caller append granular detail rows
    (per ruta / per stop / per tracking) on a second sheet so the
    operator gets the full breakdown without opening 5 attachments.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_stem) or "DS_digest"
    today = datetime.now().strftime("%Y%m%d")
    target = output_dir / f"{safe_stem}_{today}.xlsx"

    summary_rows: list[dict[str, Any]] = []
    for dsp in sorted(by_dsp.keys()):
        record = by_dsp[dsp] or {}
        row: dict[str, Any] = {"DSP": dsp}
        for col in columns:
            row[col.label] = record.get(col.key, "")
        summary_rows.append(row)
    if not summary_rows:
        return None
    summary_df = pd.DataFrame(summary_rows)

    try:
        with pd.ExcelWriter(target, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="Resumen", index=False)
            if extra_rows_per_dsp:
                detail_rows: list[dict[str, Any]] = []
                for dsp, rows in extra_rows_per_dsp.items():
                    for row in rows or []:
                        detail_rows.append({"DSP": dsp, **row})
                if detail_rows:
                    pd.DataFrame(detail_rows).to_excel(
                        writer, sheet_name="Detalle", index=False
                    )
    except Exception:  # noqa: BLE001
        _log.exception("station_digest: failed to write %s", target)
        return None
    return target


# ---------------------------------------------------------------------------
# Convenience send helper
# ---------------------------------------------------------------------------


@dataclass
class StationSendOutcome:
    sent: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self, *, channel: str, scope: str = "STATION") -> dict[str, Any]:
        status = "success" if not self.failed else ("partial" if self.sent else "error")
        return {
            "status": status,
            "channel": channel,
            "scope": scope,
            "sent": self.sent,
            "failed": self.failed,
            "targets": ["DS"],
        }


__all__ = [
    "StationColumn",
    "StationSendOutcome",
    "build_email_digest",
    "build_excel_digest",
    "build_slack_digest",
    "resolve_ds_email",
    "resolve_ds_slack",
]
