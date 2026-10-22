from __future__ import annotations

import os
import re
import threading
import warnings
from pathlib import Path
from typing import Iterable


MULTI_VALUE_ENV_KEYS = {"DOUBLESTATION_CODE", "STATION_CODES"}


# Keys that must come from ``.env`` only. The workbook is allowed to
# define its own values for everything operational (DSPs, webhooks,
# dashboards), but infrastructure pointers like the workbook path
# itself or the shared-drive root must stay under operator control.
# A workbook is allowed to set them when running on a brand-new
# station, but values already coming from ``.env`` are never
# overwritten by the workbook.
ENV_PROTECTED_KEYS = {
    "CONFIG_TOOLBOX",
    "TOOLBOX_CONFIG_XLSX",
    "CONFIG_TOOLBOC",
    "TOOLBOX_SHARED_DRIVE_ROOT",
    "SHARED_DRIVE_ROOT",
    "Z_DRIVE_ROOT",
    "BASE_AM",
    "BASE_PM",
    "BASE_AM_TB",
    "BASE_PM_TB",
    "BASE_NS_TB",
    "BASE_DO_TB",
    "BASE_XMDO",
    "BASE_ATLAS",
    "BASE_PHR",
    "BASE_FP",
    "BASE_MANIFEST",
    "BASE_TOOLBOX",
    "TOOLBOX_CORS_ORIGINS",
    "FIREFOX_PROFILE_DIR",
    "TOOLBOX_SHAREPOINT_SITE_URL",
    "SHAREPOINT_SITE_URL",
    "TOOLBOX_SHAREPOINT_LIBRARY",
}


# Aliases that point to the same setting. When any member is locked
# from .env, every alias is locked together so a workbook cannot
# inject a stale value through one of them.
ENV_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"CONFIG_TOOLBOX", "TOOLBOX_CONFIG_XLSX", "CONFIG_TOOLBOC"}),
    frozenset({"TOOLBOX_SHARED_DRIVE_ROOT", "SHARED_DRIVE_ROOT", "Z_DRIVE_ROOT"}),
    frozenset({"TOOLBOX_SHAREPOINT_SITE_URL", "SHAREPOINT_SITE_URL"}),
)


def _expand_aliases(keys: Iterable[str]) -> set[str]:
    out: set[str] = set()
    keys_upper = {str(k).upper() for k in keys}
    for key in keys_upper:
        out.add(key)
        for group in ENV_ALIAS_GROUPS:
            if key in group:
                out.update(group)
    return out


def clean_env_value(value: object) -> str:
    raw = str(value or "").strip()
    low = raw.lower()
    if len(raw) >= 3 and low.startswith("r\"") and raw.endswith("\""):
        return raw[2:-1]
    if len(raw) >= 3 and low.startswith("r'") and raw.endswith("'"):
        return raw[2:-1]
    if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
        return raw[1:-1].strip()
    return raw


def merge_env_values(key: str, current: str, incoming: str) -> str:
    key_norm = str(key or "").strip().upper()
    cur = str(current or "").strip()
    new = str(incoming or "").strip()

    if key_norm not in MULTI_VALUE_ENV_KEYS:
        return new
    if not cur:
        return new
    if not new:
        return cur

    parts: list[str] = []
    seen = set()
    for raw in re.split(r"[;,|\n]+", f"{cur},{new}"):
        token = str(raw or "").strip()
        if not token:
            continue
        up = token.upper()
        if up in seen:
            continue
        seen.add(up)
        parts.append(token)
    return ",".join(parts)


def parse_env_file(env_path: str | Path) -> dict[str, str]:
    path = Path(env_path)
    values: dict[str, str] = {}
    if not path.exists():
        return values

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or "=" in key or "\x00" in key:
                continue
            values[key] = clean_env_value(value)
    return values


def parse_env_workbook(workbook_path: str | Path) -> dict[str, str]:
    path = Path(workbook_path)
    values: dict[str, str] = {}
    if not path.exists():
        return values

    from openpyxl import load_workbook

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Workbook contains no default style, apply openpyxl's default",
            category=UserWarning,
        )
        workbook = load_workbook(path, data_only=True, read_only=True)

    try:
        for worksheet in workbook.worksheets:
            if str(worksheet.title or "").strip().lower() == "readme":
                continue
            rows = worksheet.iter_rows(values_only=True)
            header = next(rows, None)
            if not header:
                continue

            normalized_headers = [str(cell or "").strip().lower() for cell in header]
            key_idx = None
            value_idx = None
            for idx, name in enumerate(normalized_headers):
                if name in {"key", "env_key", "variable", "name"} and key_idx is None:
                    key_idx = idx
                if name in {"value", "env_value", "valor"} and value_idx is None:
                    value_idx = idx

            if key_idx is None or value_idx is None:
                continue

            for row in rows:
                if not row:
                    continue
                key = str(row[key_idx] or "").strip() if key_idx < len(row) else ""
                if not key or key.startswith("#"):
                    continue
                value = str(row[value_idx]) if value_idx < len(row) and row[value_idx] is not None else ""
                existing_key = next((item for item in values if item.upper() == key.upper()), key)
                values[existing_key] = merge_env_values(
                    existing_key,
                    values.get(existing_key, ""),
                    clean_env_value(value),
                )
    finally:
        workbook.close()

    return values


# ---------------------------------------------------------------------------
# Memoized config-workbook reader (per path + mtime/size)
# ---------------------------------------------------------------------------
# Tool modules (AM/PM wave plan, cierre) look up dozens of ``_env`` keys per
# run. Parsing the workbook with openpyxl on every lookup made a single run
# re-read the file 60-100+ times, which dominated the run duration and could
# stall the ``upload-run`` request long enough for the SPA to surface a
# spurious "no se pudo contactar el servidor". Caching by (mtime, size)
# parses the workbook at most once per change while staying fresh across
# config edits (a SharePoint re-sync replaces the cache file, bumping mtime).

_CONFIG_VALUES_LOCK = threading.Lock()
# key: absolute path -> (mtime, size, {KEY_UPPER: value})
_CONFIG_VALUES_CACHE: dict[str, tuple[float, int, dict[str, str]]] = {}


def config_workbook_values_cached(workbook_path: str | Path | None) -> dict[str, str]:
    """Return ``{KEY_UPPER: value}`` for a config workbook, memoized by mtime.

    Thread-safe. Returns an empty dict when the path is missing/unreadable.
    The returned dict is read-only shared state: callers must NOT mutate it
    in place (they build derived dicts or only read, which is safe).
    """
    if not workbook_path:
        return {}
    path = Path(workbook_path)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = str(path)
    signature = (stat.st_mtime, stat.st_size)

    with _CONFIG_VALUES_LOCK:
        entry = _CONFIG_VALUES_CACHE.get(key)
        if entry is not None and (entry[0], entry[1]) == signature:
            return entry[2]

    # Parse outside the lock (openpyxl I/O can be slow); the worst case
    # under a race is two concurrent parses of the same file, which is
    # harmless and still far cheaper than parsing on every _env lookup.
    try:
        parsed = parse_env_workbook(path)
    except Exception:  # noqa: BLE001 - unreadable/locked workbook
        return {}
    values = {str(k).strip().upper(): str(v or "") for k, v in parsed.items()}

    with _CONFIG_VALUES_LOCK:
        _CONFIG_VALUES_CACHE[key] = (signature[0], signature[1], values)
    return values


def invalidate_config_values_cache() -> None:
    """Drop all memoized config-workbook values (e.g. after a config save)."""
    with _CONFIG_VALUES_LOCK:
        _CONFIG_VALUES_CACHE.clear()


# ---------------------------------------------------------------------------
# Workbook-applied env tracking (station-switch hygiene)
# ---------------------------------------------------------------------------
# ``os.environ`` is a process-wide accumulator: keys applied from the
# PREVIOUS station's workbook (WAVE_*, <SHIFT>_TEAM_*, SLACK_*, WAVEPLAN_PM_*
# ...) survive a station switch because the reapply path only SETS keys and
# never unsets the ones that existed only in the old workbook. We track
# which keys each applied workbook contributed, plus the pre-workbook
# baseline (.env + links.env), so a switch can restore the baseline value
# (e.g. links.env-seeded C3_*/FMC_* URLs) or drop the key entirely — the
# new station then starts truly blank instead of showing the old DS's data.

_WORKBOOK_ENV_LOCK = threading.Lock()
_WORKBOOK_APPLIED_KEYS: set[str] = set()  # UPPER-case keys applied from a workbook
_PRE_WORKBOOK_BASELINE: dict[str, str] = {}  # UPPER key -> value before any workbook
_BASELINE_TAKEN = False


def snapshot_pre_workbook_baseline() -> None:
    """Capture ``os.environ`` BEFORE the first workbook layer is applied.

    First call wins (boot order: .env -> links.env -> workbook layers);
    later calls are no-ops so a re-entrant ``load_backend_env`` (tests)
    cannot poison the baseline with workbook values already in env.
    """
    global _BASELINE_TAKEN
    with _WORKBOOK_ENV_LOCK:
        if _BASELINE_TAKEN:
            return
        _BASELINE_TAKEN = True
        _PRE_WORKBOOK_BASELINE.clear()
        for key, value in os.environ.items():
            _PRE_WORKBOOK_BASELINE[str(key).upper()] = str(value)


def record_workbook_applied_keys(keys: Iterable[str]) -> None:
    """Remember which env keys were applied from a config workbook."""
    with _WORKBOOK_ENV_LOCK:
        _WORKBOOK_APPLIED_KEYS.update(str(k).strip().upper() for k in keys if str(k).strip())


def purge_stale_workbook_env(new_keys: Iterable[str]) -> int:
    """Drop workbook-applied env keys the new workbook no longer defines.

    ``new_keys`` is the set of keys (UPPER) that the NEW station's workbook
    defines with a non-blank value. Every previously workbook-applied key
    outside that set is restored to its pre-workbook baseline value when it
    had one (.env / links.env seed) or removed from ``os.environ``.
    ``ENV_PROTECTED_KEYS`` (machine/infra pointers) are never touched.

    Returns the number of keys restored/removed and resets the tracking set
    to the new workbook's keys.
    """
    keep = {str(k).strip().upper() for k in new_keys if str(k).strip()}
    protected = _expand_aliases(ENV_PROTECTED_KEYS)
    removed = 0
    with _WORKBOOK_ENV_LOCK:
        stale = [k for k in _WORKBOOK_APPLIED_KEYS if k not in keep and k not in protected]
        for key in stale:
            baseline = _PRE_WORKBOOK_BASELINE.get(key, "")
            try:
                if str(baseline).strip():
                    os.environ[key] = baseline
                else:
                    os.environ.pop(key, None)
                removed += 1
            except (ValueError, OSError):  # pragma: no cover - defensive
                continue
        _WORKBOOK_APPLIED_KEYS.difference_update(stale)
        _WORKBOOK_APPLIED_KEYS.update(keep - protected)
    return removed


def apply_env_values(values: dict[str, str], override: bool = True) -> None:
    for key, value in values.items():
        text = str(value)
        # Empty workbook cells must NOT clobber a value that was
        # already supplied by an earlier loader (links.env, .env,
        # process env). This is critical for the SCHEMA_VERSION=6
        # Mercury_C3 sheet: the workbook ships with C3_* and FMC_*
        # cells blank, expecting links.env (or the operator) to seed
        # them at runtime. Overwriting with "" would erase the
        # links.env defaults and break every tool that reads them.
        if not text.strip() and key in os.environ and os.environ[key]:
            continue
        if override or key not in os.environ:
            try:
                os.environ[key] = text
            except (ValueError, OSError):
                # Windows limita las variables de entorno a 32767 caracteres:
                # una celda enorme del workbook (listas de tareas, etc.) no
                # debe tumbar el arranque. El valor sigue disponible para los
                # lectores que parsean la hoja directamente.
                continue


def reapply_config_workbook(workbook_path: str | Path) -> int:
    """Re-apply a freshly synced config workbook into ``os.environ``.

    The process applies the config workbook to ``os.environ`` once at boot
    (``load_backend_env``). When another operator edits the shared config
    on SharePoint, a *running* instance keeps stale values until it
    restarts. Calling this after a fresh SharePoint download (on
    connect/reconnect, or when the operator opens the config) re-applies
    the shared workbook so every tool picks up the change live.

    Machine/infra pointers in ``ENV_PROTECTED_KEYS`` (the workbook path
    itself, the SharePoint site URL, the Firefox profile, the BASE_*
    roots) are deliberately left untouched — only operational config
    (DSPs, webhooks, schedules, Mercury/C3 URLs, schedules) is refreshed.

    Returns the number of keys applied (0 when the file is missing,
    unreadable, or carried no usable keys).
    """
    path = Path(workbook_path)
    if not path.exists():
        return 0
    try:
        workbook_values = parse_env_workbook(path)
    except Exception:
        return 0
    protected = _expand_aliases(ENV_PROTECTED_KEYS)
    filtered = {
        key: value
        for key, value in workbook_values.items()
        if key.upper() not in protected
    }
    # Station-switch hygiene: keys applied from a PREVIOUS workbook that the
    # freshly synced one no longer defines (different station, or a row that
    # another operator deleted/blanked) are restored to their pre-workbook
    # baseline or dropped. This runs even when the new workbook is blank
    # (brand-new station): everything from the old DS must go. A missing or
    # unreadable file (early returns above) deliberately purges nothing.
    non_blank = {k.upper() for k, v in filtered.items() if str(v or "").strip()}
    purge_stale_workbook_env(non_blank)
    if not filtered:
        return 0
    apply_env_values(filtered, override=True)
    return len(filtered)


def parse_allowed_dsps(raw: object) -> list[str]:
    dsps: list[str] = []
    seen = set()
    for item in re.split(r"[|,;]+", str(raw or "")):
        dsp = item.strip().upper()
        if not dsp or dsp in seen:
            continue
        seen.add(dsp)
        dsps.append(dsp)
    return dsps


def normalize_env_token(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")


def is_slack_webhook_url(url: object) -> bool:
    text = str(url or "").strip().lower()
    return bool(text) and ("hooks.slack" in text or "slack.com" in text)


def detect_site_code(*values: object, fallback: str = "") -> str:
    candidates: list[str] = []
    for raw in values:
        text = str(raw or "").upper()
        if not text:
            continue
        candidates.extend(re.findall(r"\b[A-Z]{3}\d\b", text))
    if candidates:
        return normalize_env_token(candidates[0])
    return normalize_env_token(fallback)


def is_hub_route(route: object) -> bool:
    text = str(route or "").strip().upper()
    if not text:
        return False
    for prefix in ("MRU", "MRG", "MR"):
        if text == prefix or text.startswith(f"{prefix}_") or text.startswith(f"{prefix}-"):
            return True
    return False


def _candidate_env_tokens(value: object) -> list[str]:
    normalized = normalize_env_token(value)
    if not normalized:
        return []

    out: list[str] = []
    seen = set()

    def add(token: str) -> None:
        token = token.strip("_")
        if not token or token in seen:
            return
        seen.add(token)
        out.append(token)

    add(normalized)
    compact = normalized.replace("_", "")
    if compact != normalized:
        add(compact)

    parts = [part for part in normalized.split("_") if part]
    if len(parts) >= 2:
        merged: list[str] = []
        idx = 0
        while idx < len(parts):
            cur = parts[idx]
            nxt = parts[idx + 1] if idx + 1 < len(parts) else ""
            if cur.isalpha() and nxt.isdigit():
                merged.append(f"{cur}{nxt}")
                idx += 2
            else:
                merged.append(cur)
                idx += 1
        merged_token = "_".join(merged)
        add(merged_token)
        add(merged_token.replace("_", ""))

    return out


def resolve_slack_webhook_for_dsp(
    dsp: object,
    site_hint: object = "",
    env_map: dict[str, str] | None = None,
    common_keys: Iterable[str] | None = None,
    variant: object = "",
) -> str:
    dsp_keys = _candidate_env_tokens(dsp)
    site_keys = _candidate_env_tokens(site_hint)
    if not dsp_keys:
        return ""

    # Per-DSP channel variant ("URGENTES", "PERFO", ...) selected in the
    # "Comunicaciones" picker. When the caller does not pass one
    # explicitly, use the variant of the tool currently running (set by
    # ``run_tool``). The variant key is tried FIRST, then the resolution
    # falls back to the regular ``SLACK_<DSP>`` chain.
    variant_token = normalize_env_token(variant)
    if not variant_token:
        try:
            from app.core.tool_context import get_current_dsp_variant

            variant_token = normalize_env_token(get_current_dsp_variant())
        except Exception:  # noqa: BLE001 - resolution must never break a send
            variant_token = ""

    def read_env(key: str) -> str:
        if env_map is not None:
            for current_key, current_value in env_map.items():
                if current_key.strip().upper() == key.strip().upper():
                    return str(current_value or "").strip()
            return ""
        return str(os.environ.get(key, "") or "").strip()

    keys: list[str] = []
    base_keys: list[str] = []
    for dsp_key in dsp_keys:
        dsp_parts = [part for part in dsp_key.split("_") if part]
        base_key = dsp_parts[0] if dsp_parts else dsp_key

        if variant_token:
            keys.append(f"SLACK_{dsp_key}_{variant_token}")
            if base_key != dsp_key:
                keys.append(f"SLACK_{base_key}_{variant_token}")

        for site_key in site_keys:
            if not site_key:
                continue
            if dsp_key.endswith(f"_{site_key}") or dsp_key.endswith(site_key):
                continue
            keys.append(f"SLACK_{dsp_key}_{site_key}")
            keys.append(f"SLACK_{base_key}_{site_key}")

        keys.append(f"SLACK_{dsp_key}")
        if base_key != dsp_key:
            base_keys.append(f"SLACK_{base_key}")

    keys.extend(base_keys)
    seen = set()
    ordered_keys = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        ordered_keys.append(key)

    for key in ordered_keys:
        url = read_env(key)
        if is_slack_webhook_url(url):
            return url

    for key in common_keys or ("COMMON_SLACK", "SLACK_DSP_PERFO", "SLACK_DS"):
        url = read_env(str(key))
        if is_slack_webhook_url(url):
            return url
    return ""


def split_slack_message(message: object, limit: int = 3900) -> list[str]:
    text = str(message or "")
    if limit <= 0 or len(text) <= limit:
        return [text]

    out: list[str] = []
    current: list[str] = []
    current_len = 0
    for raw_line in text.splitlines():
        line = str(raw_line)
        if len(line) > max(1, limit - 10):
            step = max(1, limit - 10)
            if current:
                out.append("\n".join(current))
                current = []
                current_len = 0
            out.extend(line[idx : idx + step] for idx in range(0, len(line), step))
            continue

        needed = len(line) + (1 if current else 0)
        if current and current_len + needed > limit:
            out.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += needed

    if current:
        out.append("\n".join(current))
    return out or [text]


def get_case_insensitive(values: dict[str, str], key: str) -> str:
    wanted = key.strip().upper()
    for current_key, current_value in values.items():
        if current_key.strip().upper() == wanted:
            return str(current_value or "").strip()
    return ""


def load_backend_env(config_dir: str | Path) -> dict[str, object]:
    """Load env values with the following priority (later wins):

    1. ``backend/config/.env`` — static developer defaults + CONFIG_TOOLBOX pointer.
    2. ``backend/config/links.env`` — deployment-shared URLs (station-agnostic).
    3. ``backend/config/toolbox_config.xlsx`` — local copy, if present.
    4. Shared-drive workbook referenced by ``CONFIG_TOOLBOX`` / ``TOOLBOX_CONFIG_XLSX``
       env keys. This is the production source of truth (DSPs, webhooks, etc.).
    5. ``test/toolbox_config_test.xlsx`` — only as fallback when none of the above
       is reachable (useful for laptops without Z: drive).
    """
    config_path = Path(config_dir)
    dotenv_path = config_path / ".env"
    links_env_path = config_path / "links.env"
    workbook_path = config_path / "toolbox_config.xlsx"

    values: dict[str, str] = {}
    source_parts: list[str] = []

    # Track which keys were set by ``.env`` so workbook layers below
    # cannot overwrite the protected ones (CONFIG_TOOLBOX, BASE_*, etc.).
    locked_from_env: set[str] = set()

    def _apply_workbook(path: Path, source_tag: str) -> bool:
        try:
            workbook_values = parse_env_workbook(path)
        except Exception:
            return False
        if not workbook_values:
            return False
        # Capture the pre-workbook env (idempotent, first call wins) so a
        # later station switch can restore .env/links.env-seeded values.
        snapshot_pre_workbook_baseline()
        # Strip every protected key the .env already locked in. The
        # workbook can still introduce them on a brand-new station.
        filtered = {
            k: v
            for k, v in workbook_values.items()
            if k.upper() not in locked_from_env
        }
        if not filtered:
            return False
        values.update(filtered)
        apply_env_values(filtered, override=True)
        # Track the applied keys so a station switch can purge the ones the
        # new station's workbook does not define (see purge_stale_workbook_env).
        record_workbook_applied_keys(
            k for k, v in filtered.items() if str(v or "").strip()
        )
        source_parts.append(f"{source_tag}:{path}")
        return True

    if dotenv_path.exists():
        dotenv_values = parse_env_file(dotenv_path)
        values.update(dotenv_values)
        apply_env_values(dotenv_values, override=True)
        # Lock every protected key actually set by .env so subsequent
        # workbook layers don't redefine them with stale values.
        seeded_protected: set[str] = set()
        for key, value in dotenv_values.items():
            if not str(value or "").strip():
                continue
            if key.upper() in ENV_PROTECTED_KEYS:
                seeded_protected.add(key.upper())
        if seeded_protected:
            locked_from_env.update(_expand_aliases(seeded_protected))
        source_parts.append(f"env:{dotenv_path}")

    # links.env carries the URLs that are the same across every DS
    # (Logistics, PerfectMile, river.amazon.com tickets, TT_*, Cierre,
    # TOOLBOX_*_LINKS, ASANA, WIKI). It ships with the repo and is
    # loaded before the per-DS workbook so the workbook can still
    # override any individual URL when a station truly needs it.
    if links_env_path.exists():
        links_values = parse_env_file(links_env_path)
        values.update(links_values)
        apply_env_values(links_values, override=True)
        seeded_protected: set[str] = set()
        for key, value in links_values.items():
            if not str(value or "").strip():
                continue
            if key.upper() in ENV_PROTECTED_KEYS:
                seeded_protected.add(key.upper())
        if seeded_protected:
            locked_from_env.update(_expand_aliases(seeded_protected))
        source_parts.append(f"links-env:{links_env_path}")

    # Baseline for station-switch purges: the env as seeded by .env +
    # links.env, BEFORE any workbook layer. Captured unconditionally so a
    # boot without a workbook (brand-new install) still records it for the
    # first station the operator configures later.
    snapshot_pre_workbook_baseline()

    # The local ``backend/config/toolbox_config.xlsx`` is a DEV artifact: it
    # ships with sample data (e.g. STATION_CODE, a placeholder email, a demo
    # DSP). When SharePoint is the config store we must NOT apply it as an
    # env layer — otherwise its stale values leak into ``os.environ`` and
    # SharePoint's empty cells never clear them (the operator then sees a
    # wrong station / email / DSP that "keeps coming back"). SharePoint is
    # the sole source in that case.
    _sharepoint_site_set = bool(
        (os.environ.get("TOOLBOX_SHAREPOINT_SITE_URL") or "").strip()
        or (os.environ.get("SHAREPOINT_SITE_URL") or "").strip()
    )
    if workbook_path.exists() and not _sharepoint_site_set:
        _apply_workbook(workbook_path, "local-xlsx")

    # Production source of truth: the per-station config workbook on
    # SharePoint (``<library>/<STATION>/Config/toolbox_config.xlsx``).
    # This replaces the old shared-drive (Z:) workbook. We download it
    # into the local cache and parse it; ``CONFIG_TOOLBOX`` is pointed at
    # that cache path so config_routes can read/edit it and upload changes
    # back via ``sharepoint_storage``.
    production_loaded = False
    sp_config_loaded = False
    try:
        from app.shared_excel import sharepoint_storage as sps

        if sps.is_configured():
            cache = sps.sync_config_down()
            if cache is not None and cache.exists():
                if _apply_workbook(cache, "sharepoint-xlsx"):
                    production_loaded = True
                    sp_config_loaded = True
                    os.environ["CONFIG_TOOLBOX"] = str(cache)
            else:
                # No config on SharePoint for this station yet. Do NOT seed
                # it from the local dev ``toolbox_config.xlsx`` (sample data:
                # placeholder email, demo DSP, stale STATION_CODE) — that
                # would pollute the DS. Just provision the per-DS folder; a
                # blank template is created on first read/save.
                try:
                    sps.ensure_station_folder()
                except Exception:  # noqa: BLE001 — offline: created on first save
                    pass
                os.environ["CONFIG_TOOLBOX"] = str(sps.config_cache_path())
                sp_config_loaded = True
    except Exception:  # noqa: BLE001 — SharePoint offline/no session.
        # SharePoint unreachable (offline, expired session, locked cache
        # file...). Fall back to the last-known-good cached copy of the
        # station workbook so config keys (NOVA_API_KEY, ALLOWED_DSPS,
        # DPR_*...) do not silently vanish from the environment — that
        # made tools misbehave with a partial config instead of failing
        # loudly. The cache is the real per-station workbook synced on a
        # previous run, never the local dev sample.
        sp_config_loaded = False
        try:
            from app.shared_excel import sharepoint_storage as sps

            stale_cache = sps.config_cache_path()
            if stale_cache.exists() and _apply_workbook(stale_cache, "sharepoint-xlsx-stale"):
                production_loaded = True
                sp_config_loaded = True
                os.environ["CONFIG_TOOLBOX"] = str(stale_cache)
        except Exception:  # noqa: BLE001 — cache also unusable; boot anyway.
            pass

    # Legacy / local fallback: ONLY when SharePoint is NOT configured (pure
    # dev without a SharePoint site). When a SharePoint site is set, the
    # config comes exclusively from SharePoint — we never apply the local
    # ``toolbox_config.xlsx``, a Z: workbook or the test workbook, so no
    # sample/stale data can leak into the running config.
    if not sp_config_loaded and not _sharepoint_site_set:
        for env_key in (
            "CONFIG_TOOLBOX",
            "TOOLBOX_CONFIG_XLSX",
            "CONFIG_TOOLBOC",  # legacy typo present in old .env files
        ):
            raw = os.environ.get(env_key, "").strip().strip('"').strip("'")
            if not raw:
                continue
            prod_path = Path(raw).expanduser()
            try:
                exists = prod_path.exists()
            except OSError:
                # Network share temporarily unavailable (e.g. Z: timeout).
                exists = False
            if not exists:
                continue
            if _apply_workbook(prod_path, "shared-xlsx"):
                production_loaded = True
                os.environ["CONFIG_TOOLBOX"] = str(prod_path)
                break

    # Test fallback: only for a pure-dev run with no SharePoint site.
    if not production_loaded and not _sharepoint_site_set:
        try:
            project_root = config_path.resolve().parents[1]
        except IndexError:
            project_root = None
        if project_root is not None:
            test_workbook = project_root / "test" / "toolbox_config_test.xlsx"
            if test_workbook.exists():
                _apply_workbook(test_workbook, "test-xlsx")

    # The active station is authoritative from the state file (the DS folder
    # the operator selected), NEVER the ``STATION_CODE`` cell inside the
    # workbook — that cell can be stale/polluted (e.g. DCZ3's config having
    # STATION_CODE=DCZ4 inside), which otherwise makes the dashboard/tools
    # show a different DS than the one actually loaded. Force it here so
    # every consumer (settings, health, tools) agrees with the folder.
    try:
        from app.shared_excel import sharepoint_storage as _sps_active

        _active_station = _sps_active.read_active_station()
        if _active_station:
            os.environ["STATION_CODE"] = _active_station
    except Exception:  # noqa: BLE001 - never break boot on this
        pass

    # Backwards-compat aliases — SCHEMA_VERSION=4 dropped a handful of
    # cells whose value was 100% duplicated from a canonical key. Code
    # that still reads the old name keeps working because we mirror
    # the canonical value into ``os.environ`` under the legacy alias.
    # The mirror is one-way and idempotent.
    _apply_compat_aliases()

    return {
        "source": "backend_config" if source_parts else "existing",
        "source_path": ";".join(source_parts),
        "values": values,
        "workbook_path": str(workbook_path) if workbook_path.exists() else "",
        "production_loaded": production_loaded,
    }


# ---------------------------------------------------------------------------
# Compat aliases — keep legacy reads working after SCHEMA_VERSION=4 dedup.
# ---------------------------------------------------------------------------


# Legacy KEY -> canonical KEY. The runtime mirrors the canonical value
# into the legacy key in ``os.environ`` so code that still reads the
# old name keeps functioning. The legacy key is NEVER persisted back
# to the workbook (the importer drops it; the schema does not declare
# it any more).
_COMPAT_ALIASES: dict[str, str] = {
    "CIERRE_PENDIENTES_URL": "LOGISTICS_AGEING_URL",
    "CIERRE_DEBRIEF_URL": "LOGISTICS_AGEING_URL",
    "CIERRE_RETURNS_URL_TEMPLATE": "PERFECTMILE_RETURNS_SIMPLE_URL",
    "CIERRE_CORTEX_URL_TEMPLATE": "LOGISTICS_ITINERARIES_URL_TEMPLATE",
    # Mercury DINO casing variants used in legacy workbooks.
    "C3_DINO_MERCURY": "C3_MERCURY_DINO",
    "C3_DELIVERED_DASHBOARD_URL": "C3_DELIVERED_MERCURY_URL",
}


def _apply_compat_aliases() -> None:
    """Mirror canonical env values into their legacy alias names.

    Only fills the legacy key when:
      * the legacy key is currently empty / unset, AND
      * the canonical key is set.

    This way an operator who explicitly typed a different value into
    a legacy cell (very rare but possible) is still respected.
    """
    for legacy, canonical in _COMPAT_ALIASES.items():
        existing = str(os.environ.get(legacy, "") or "").strip()
        if existing:
            continue
        canonical_value = str(os.environ.get(canonical, "") or "").strip()
        if not canonical_value:
            continue
        os.environ[legacy] = canonical_value
