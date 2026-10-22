"""Catalog of Mercury queries indexed by country / station / process.

The canonical source is ``backend/config/mercury_queries.json``.
Operators can extend it (or override per environment) by setting
``MERCURY_QUERIES_FILE`` to an alternative JSON file. The structure is:

```
{
  "default_region": "eu",
  "default_country": "ES",
  "default_station": "DCZ4",
  "queries": {
    "ES": {
      "DCZ4": {
        "dsc_mailbox": {
          "id": "926ce4d31139a1705c41c282aef7d8d2",
          "region": "eu",
          "label": "DSC Mailbox",
          "expected_columns": [...],
          "column_map": { "trackingId": "tracking_id", ... }
        }
      }
    }
  }
}
```

The ``column_map`` (optional) lets each consumer translate the raw
Mercury column names into the canonical keys their downstream pipeline
expects (e.g. ``trackingId`` -> ``tracking_id``).

Per-DS overrides
----------------

Mercury query IDs change per DS / per process. Instead of forcing the
operator to edit ``mercury_queries.json`` (which lives inside the
bundled exe and is read-only), the schema's ``Mercury_C3`` sheet
declares one URL field per query (``C3_DSC_MAILBOX_URL``,
``C3_DSC_DOORSTEP_URL``, ``C3_MERCURY_DINO``, ...). When such a field
is set in the workbook (or via the quick-update endpoint), this
module parses the ``ID`` from the URL and uses it instead of the
JSON default. ``region`` is also extracted when present.

The mapping ``query_key -> env_var`` lives in the schema fields
(``mercury_query_key``); we re-build it lazily so adding a new
``Field(..., mercury_query_key="...")`` is enough for both the
quick-update wizard and the autoretrieval path.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


# Module-level cache so we do not hit disk on every fetch.
_LOGGER = logging.getLogger(__name__)

_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float = 0.0


# Pre-compiled regex for the query id (``getQueryResponse?ID=``, the
# newer "Refreshable Excel" ``getWebQueryData?metricId=`` and the older
# ``getReport?id=``). The id is only used for diagnostics/cache identity
# and for catalog-only queries: a pasted link is fetched AS-IS (see
# :func:`normalize_mercury_url`), so its style never decides the data.
_ID_PARAM_RE = re.compile(r"[?&](?:id|metricid)=([A-Za-z0-9]+)", re.IGNORECASE)

# Copying a link from a rendered page/email turns ``&reg`` into the
# HTML entity ``®`` (U+00AE), leaving ``…®ion=eu``. Restore it before
# parsing so the region survives.
_MANGLED_REGION_RE = re.compile("\u00aeion=", re.IGNORECASE)

# c3.amazon.com is deprecated (2026-09-01): same service, now on
# mercury.amazon.com. Links copied from c3 keep their path and params;
# only the host is moved so the Mercury session cookies apply.
_DEPRECATED_HOSTS = {"c3.amazon.com": "mercury.amazon.com"}


def normalize_mercury_url(url: str) -> str:
    """Return the pasted Mercury link cleaned up for a direct fetch.

    Repairs the ``®ion=`` copy artefact, moves the deprecated
    ``c3.amazon.com`` host to ``mercury.amazon.com`` and keeps the path
    and every query parameter untouched. Returns ``""`` when the value
    is not an http(s) URL.
    """
    text = (url or "").strip().strip('"').strip("'")
    if not text:
        return ""
    text = _MANGLED_REGION_RE.sub("&region=", text)
    try:
        parsed = urlparse(text)
    except Exception:  # noqa: BLE001
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    new_host = _DEPRECATED_HOSTS.get(host, host)
    netloc = new_host if parsed.port is None else f"{new_host}:{parsed.port}"
    return parsed._replace(scheme="https", netloc=netloc).geturl()


def _parse_mercury_url(url: str) -> tuple[str, str]:
    """Return ``(id, region)`` extracted from a Mercury URL.

    Accepted styles (all equivalent for the same saved query):
    ``https://mercury.amazon.com/getQueryResponse?ID=<hex>&region=eu``,
    ``https://mercury.amazon.com/getWebQueryData?metricId=<hex>&region=eu-south-2``
    (also on the deprecated c3.amazon.com host). We tolerate either
    casing of the parameter and missing fields. Returns empty strings
    when nothing usable is found.
    """
    text = (url or "").strip().strip('"').strip("'")
    if not text:
        return "", ""
    text = _MANGLED_REGION_RE.sub("&region=", text)
    try:
        parsed = urlparse(text)
        params = parse_qs(parsed.query, keep_blank_values=False)
        # ``parse_qs`` is case-sensitive; normalise.
        flat: dict[str, str] = {}
        for key, values in params.items():
            if not values:
                continue
            flat[key.lower()] = values[0]
        ident = flat.get("id", "") or flat.get("metricid", "")
        region = flat.get("region", "")
        if not ident:
            match = _ID_PARAM_RE.search(text)
            if match:
                ident = match.group(1)
        return ident.strip(), region.strip().lower()
    except Exception:  # noqa: BLE001
        match = _ID_PARAM_RE.search(text)
        return (match.group(1) if match else ""), ""


def _query_key_to_env_var() -> dict[str, str]:
    """Return ``{mercury_query_key: ENV_VAR_NAME}`` from the schema.

    Imported lazily to avoid a circular import (the schema module
    imports nothing from us, but the integration tree boots before
    the schema dataclass is fully built in some test contexts).
    """
    try:
        from app.shared_excel.schemas import static_fields
    except Exception:  # noqa: BLE001
        return {}
    mapping: dict[str, str] = {}
    for _sheet, field_def in static_fields():
        key = getattr(field_def, "mercury_query_key", "") or ""
        if not key or not getattr(field_def, "is_mercury", False):
            continue
        # Only the first declaration wins so a "primary" URL field
        # (Mercury_C3 sheet) overrides any placeholder reference in
        # Slots_Por_Tool that points back to it.
        mapping.setdefault(key, field_def.key)
    return mapping


def _query_key_to_default_url() -> dict[str, str]:
    """Return ``{mercury_query_key: default_url}`` from the schema.

    Some Mercury queries (e.g. ``perfo_ops_dashboard``,
    ``pnov_packages``) ship with an empty ``id`` in
    ``mercury_queries.json`` because the real query ID is meant to be
    configured per DS via a ``C3_*_URL`` workbook field. Those fields
    declare a ``default`` URL with a working ID. When the operator's
    workbook/env never seeded that value (older workbook with a blank
    row, missing ``links.env`` entry, ...), we fall back to this
    default so the query stays resolvable out-of-the-box instead of
    being silently dropped from the catalog.
    """
    try:
        from app.shared_excel.schemas import static_fields
    except Exception:  # noqa: BLE001
        return {}
    mapping: dict[str, str] = {}
    for _sheet, field_def in static_fields():
        key = getattr(field_def, "mercury_query_key", "") or ""
        if not key or not getattr(field_def, "is_mercury", False):
            continue
        default_url = getattr(field_def, "default", "") or ""
        if default_url:
            mapping.setdefault(key, default_url)
    return mapping


@dataclass(frozen=True)
class MercuryQuery:
    key: str
    country: str
    station: str
    process: str
    bucket: str
    query_id: str
    region: str
    label: str
    description: str
    expected_columns: tuple[str, ...]
    column_map: dict[str, str] = field(default_factory=dict)
    request_params: dict[str, str] = field(default_factory=dict)
    pagination: dict[str, Any] = field(default_factory=dict)
    search: dict[str, Any] = field(default_factory=dict)
    node_filter: dict[str, Any] = field(default_factory=dict)
    # The link the station pasted (normalized), fetched AS-IS: its path
    # and params are respected (``getQueryResponse``, ``getWebQueryData``
    # or whatever Mercury ships next). Empty for catalog-only queries,
    # which keep the rebuilt ``getQueryResponse?ID=`` request.
    source_url: str = ""

    @property
    def has_target(self) -> bool:
        """True when there is something fetchable (pasted URL or id)."""
        return bool(self.source_url or self.query_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "country": self.country,
            "station": self.station,
            "process": self.process,
            "bucket": self.bucket,
            "id": self.query_id,
            "url": self.source_url,
            "region": self.region,
            "label": self.label,
            "description": self.description,
            "expected_columns": list(self.expected_columns),
            "column_map": dict(self.column_map),
            "request_params": dict(self.request_params),
            "pagination": dict(self.pagination),
            "search": dict(self.search),
            "node_filter": dict(self.node_filter),
        }


def _catalog_path() -> Path:
    override = os.environ.get("MERCURY_QUERIES_FILE", "").strip().strip('"').strip("'")
    if override:
        path = Path(override)
        if path.exists():
            return path
    # Default: backend/config/mercury_queries.json
    here = Path(__file__).resolve().parents[2]
    return here / "config" / "mercury_queries.json"


def _load_catalog() -> dict[str, Any]:
    global _CACHE, _CACHE_MTIME

    path = _catalog_path()
    if not path.exists():
        _CACHE = {"queries": {}, "default_region": "eu", "default_country": "", "default_station": ""}
        _CACHE_MTIME = 0.0
        return _CACHE
    mtime = path.stat().st_mtime
    if _CACHE is not None and mtime == _CACHE_MTIME:
        return _CACHE
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        data = {"queries": {}}
    _CACHE = data if isinstance(data, dict) else {"queries": {}}
    _CACHE_MTIME = mtime
    return _CACHE


def _resolve_defaults() -> tuple[str, str, str]:
    catalog = _load_catalog()
    country = (
        os.environ.get("MERCURY_COUNTRY", "").strip().upper()
        or str(catalog.get("default_country") or "").strip().upper()
        or os.environ.get("STATION_COUNTRY", "").strip().upper()
    )
    station = (
        os.environ.get("MERCURY_STATION", "").strip().upper()
        or str(catalog.get("default_station") or "").strip().upper()
        or os.environ.get("STATION_CODE", "").strip().upper()
    )
    region = (
        os.environ.get("MERCURY_REGION", "").strip().lower()
        or str(catalog.get("default_region") or "eu").strip().lower()
    )
    return country, station, region


def list_queries(
    *,
    country: str | None = None,
    station: str | None = None,
) -> list[MercuryQuery]:
    """Return every query for the requested country/station scope.

    When ``country`` / ``station`` are omitted the catalog defaults are
    used (env override → ``default_country`` / ``default_station``).
    If no defaults match (and the caller did not supply one), we
    return **every** query in the catalog so the UI can offer the full
    list — this keeps the dashboard usable on stations that have not
    declared a scope yet.
    """
    default_country, default_station, default_region = _resolve_defaults()
    target_country = (country or default_country or "").upper()
    target_station = (station or default_station or "").upper()

    catalog = _load_catalog()
    queries_root = catalog.get("queries") or {}
    if not isinstance(queries_root, dict):
        return []

    bucket: dict[str, Any] = {}
    if target_country and target_country in queries_root and isinstance(queries_root[target_country], dict):
        country_block = queries_root[target_country]
        if target_station and isinstance(country_block.get(target_station), dict):
            bucket = country_block[target_station]
        elif isinstance(country_block.get(""), dict):
            bucket = country_block[""]

    out: list[MercuryQuery] = []
    if bucket:
        out.extend(_build_query_objects(bucket, target_country, target_station, default_region))
        return out

    # Fallback: caller did not pin a scope and we found no match for
    # the configured defaults. Surface every query in the catalog so
    # the UI keeps working without forcing manual configuration.
    if not country and not station:
        for c_key, country_block in queries_root.items():
            if not isinstance(country_block, dict):
                continue
            for s_key, station_block in country_block.items():
                if not isinstance(station_block, dict):
                    continue
                out.extend(
                    _build_query_objects(
                        station_block,
                        str(c_key).upper(),
                        str(s_key).upper(),
                        default_region,
                    )
                )
    return out


def _build_query_objects(
    block: dict[str, Any],
    country: str,
    station: str,
    default_region: str,
) -> list[MercuryQuery]:
    out: list[MercuryQuery] = []
    overrides = _query_key_to_env_var()
    default_urls = _query_key_to_default_url()
    for key, raw in block.items():
        if not isinstance(raw, dict):
            continue
        query_id = str(raw.get("id") or "").strip()
        region = str(raw.get("region") or default_region or "eu").lower()
        source_url = ""

        # Per-DS override via the workbook. ``Mercury_C3`` exposes one
        # ``C3_<KEY>_URL`` field per query that the operator can
        # update from the quick-edit wizard. When set, THAT link is what
        # gets fetched (as-is: path + params respected), whatever its
        # style. The id/region are extracted only for diagnostics.
        env_var = overrides.get(str(key))
        if env_var:
            override_url = os.environ.get(env_var, "").strip()
            if override_url:
                source_url = normalize_mercury_url(override_url)
                if not source_url:
                    # The station configured its own link but it is not
                    # a URL. Do NOT fall back to the catalog id (it
                    # belongs to another station, e.g. DCZ4) — that
                    # silently served another station's data. Drop the
                    # query so the fetch fails loudly instead.
                    _LOGGER.warning(
                        "Mercury: %s=%r is not a usable URL; ignoring catalog id for '%s'.",
                        env_var,
                        override_url[:200],
                        key,
                    )
                    continue
                override_id, override_region = _parse_mercury_url(source_url)
                # Never keep the catalog (other station) id next to the
                # station's own link.
                query_id = override_id
                if override_region:
                    region = override_region

        # Fallback: neither the JSON nor the workbook/env override gave
        # us an ID. Use the schema field's ``default`` URL (it carries a
        # working query ID) so queries like ``perfo_ops_dashboard`` stay
        # resolvable even when the operator never seeded the C3 URL.
        if not query_id and not source_url:
            default_url = default_urls.get(str(key))
            if default_url:
                default_id, default_region_override = _parse_mercury_url(default_url)
                if default_id:
                    query_id = default_id
                if default_region_override:
                    region = default_region_override

        if not query_id and not source_url and not raw.get("search"):
            continue
        out.append(
            MercuryQuery(
                key=str(key),
                country=country,
                station=station,
                process=str(raw.get("process") or "").lower(),
                bucket=str(raw.get("bucket") or "").lower(),
                query_id=query_id,
                region=region,
                label=str(raw.get("label") or key),
                description=str(raw.get("description") or ""),
                expected_columns=tuple(
                    str(col) for col in (raw.get("expected_columns") or []) if str(col)
                ),
                column_map={
                    str(k): str(v)
                    for k, v in (raw.get("column_map") or {}).items()
                    if str(k) and str(v)
                },
                request_params={
                    str(k): str(v)
                    for k, v in (raw.get("request_params") or {}).items()
                    if str(k) and str(v) != ""
                },
                pagination=dict(raw.get("pagination") or {}),
                search=dict(raw.get("search") or {}),
                node_filter=dict(raw.get("node_filter") or {}),
                source_url=source_url,
            )
        )
    return out


def find_query(
    key: str,
    *,
    country: str | None = None,
    station: str | None = None,
) -> MercuryQuery | None:
    """Return the query identified by ``key`` for the requested scope.

    Falls back to a catalog-wide search when the scope yields no match
    (mirrors :func:`list_queries`).
    """
    target = key.strip()
    if not target:
        return None
    for query in list_queries(country=country, station=station):
        if query.key == target:
            return query
    if country or station:
        for query in list_queries():
            if query.key == target:
                return query
    return None


def all_scopes() -> list[dict[str, str]]:
    """Inventory of country/station combinations declared in the catalog."""
    catalog = _load_catalog()
    queries_root = catalog.get("queries") or {}
    out: list[dict[str, str]] = []
    if not isinstance(queries_root, dict):
        return out
    for country, country_block in queries_root.items():
        if not isinstance(country_block, dict):
            continue
        for station in country_block.keys():
            out.append({"country": str(country), "station": str(station)})
    return out


__all__ = [
    "MercuryQuery",
    "normalize_mercury_url",
    "list_queries",
    "find_query",
    "all_scopes",
]
