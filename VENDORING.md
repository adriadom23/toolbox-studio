# VENDORING.md

This document states exactly what Toolbox Studio copied from the main
Toolbox repository (`c:\Users\adriadom\Desktop\ToolBox`), from where, as
of when, and how to re-sync it later. The full investigation and
reasoning behind every decision below lives in
`ToolBox\.agents\tasks\toolbox-studio\design.md` §1 (backend),
§2 (frontend) and §9 (scope cuts) — this file reproduces the parts a
developer working in Studio actually needs, rather than re-deriving them.

## Snapshot date

**2026-10-22.** Everything below reflects the state of the Toolbox repo
on that date. Toolbox keeps evolving; Studio's vendored copy does not
follow it automatically (see "Re-syncing" below).

## Source paths and file counts

| Source (in `ToolBox`) | Vendored to (in `ToolboxStudio`) | Count |
|---|---|---|
| `backend/app/integrations/` | `backend/vendor/integrations/` | 77 files — 75 `.py` + 2 `.json` sidecars (`quicksight_bridge_map.json`, `quicksight_scheduling_map.json`, read by `quicksight_bridge_client.py` / `quicksight_scheduling_client.py`) |
| `backend/app/core/{env_loader,station_context,amounts,font_prefs,watchdog,locale}.py` | `backend/vendor/core/` | 6 modules, copied verbatim |
| `backend/app/shared_excel/{sharepoint_storage,toolbox_output}.py` | **not copied** — replaced by local stubs, see decision table | 0 (2 stubs instead, `backend/vendor/support/`) |
| `frontend/src/shared/components/*.vue` | `frontend/src/shared/components/` | 34 of the 50 real files (16 excluded — see design.md §2.2) |
| `frontend/src/shared/{stores,api,styles}/...` | `frontend/src/shared/{stores,api,styles}/...` | supporting composables/CSS the 34 vendored components actually need (see design.md §2.3–§2.6); `stores/globalConnections.js` is a rewrite, not a copy — see the dedicated note below |

All `.py` and `.vue` files vendored verbatim (not listed as a stub/slim
rewrite below) are **byte-for-byte copies** of the Toolbox source at the
snapshot date above — never hand-edited. Any substitution a vendored
file needs is done via a separate shim/stub module at the exact import
path the vendored code expects, never by editing the vendored file
itself.

## Re-syncing: manual only, no automated tooling

There is no script, submodule, package registry, or CI job that keeps
this vendored copy in sync with Toolbox. Re-syncing is a deliberate,
periodic **human** task:

1. Re-run the same investigation this snapshot was built from (grep the
   current Toolbox source for the same import surfaces, re-check file
   counts, re-check which call sites are guarded).
2. Diff the result against what's vendored here.
3. Re-copy whatever changed, by hand.

This is intentional (design.md §9, scope cut #6): Studio does not build
any ToolBox-repo-writing automation, git submodules, or auto-sync
tooling for the vendored code.

## Per-module vendor / slim / stub decision table

Reproduced verbatim from `design.md` §1.2.

| Module | Lines | Used for | Decision | Reason |
|---|---|---|---|---|
| `app.core.env_loader` | 849 | `parse_allowed_dsps`, `clean_env_value` | **Vendor verbatim** | Both functions are pure, stdlib-only (`re`, `os`), zero `app.*` imports in the whole file. The file also contains `parse_env_workbook` (SharePoint-workbook env ingestion) which nothing in Studio calls, but it costs nothing to carry unused — it has no further imports — and vendoring the whole file is simpler and lower-risk than hand-extracting two functions into a new file that would then diverge from upstream. |
| `app.core.station_context` | 98 | `resolve_station_code` | **Vendor verbatim** | Fully self-contained (just a `contextvars.ContextVar`); zero `app.*` imports. Studio has one station concept (its own config), so `resolve_station_code(default=...)` degrades to always returning Studio's configured default — correct behavior with zero code change. |
| `app.core.amounts` | 94 | `scc_amount_to_eur` | **Vendor verbatim** | Pure numeric helper, zero `app.*` imports. One of the unguarded module-level imports — harmless since vendored regardless. |
| `app.core.font_prefs` | 98 | `email_font_scale` | **Vendor verbatim** | Reads a JSON preference file and env vars only; zero `app.*` imports. |
| `app.core.watchdog` | 59 | `watchdog_pause` (context manager) | **Vendor verbatim** | A no-op-capable context manager (pauses ToolBox's exe watchdog heartbeat during long I/O); zero `app.*` imports. In Studio (never run as a frozen exe with a watchdog) it is a harmless context manager that does nothing. One of the unguarded module-level imports — harmless since vendored regardless. |
| `app.core.locale` | 81 | `locale_from_request` | **Vendor verbatim** | Only imports `flask.request` and `app.integrations.messages_i18n` (already vendored as part of the integrations folder). Needed so `email_client.send_email`/`slack_client.post_message` can localize text exactly as in ToolBox. |
| `app.core.tool_destinations` | 1078 | 3 private helpers used only by `dive_deep_metrics.py`'s optional SharePoint-workbook persistence branch | **Stub / do not vendor** | The module's real purpose is ToolBox's full per-tool Slack/email destination registry tied to `app.core.tool_registry`'s entire tool catalog — nothing Studio has. Every call site in `dive_deep_metrics.py` wraps the import in `try/except Exception` and already has a working local-JSON-file fallback, so not vendoring this module does not break `dive_deep_metrics.py` at all. |
| `app.shared_excel.config_workbook` | 521 | `upsert_keyvalue_sheet`, used only inside the same guarded branch of `dive_deep_metrics.py` | **Stub / do not vendor** | Reached only through the `try/except` branch gated by a helper that is itself stubbed away, so this import is never reached in practice. Also owns `VERSION_ADMIN_LOGINS`/`version_change_allowed()` — ToolBox's own release-version protection, not something Studio should carry (Studio has its own, simpler `owner_config.py`). |
| `app.shared_excel.toolbox_output` | 817 | `get_base_toolbox_dir` (used by `manifest_lookup.py` to find a local manifest cache dir) | **Stub a 5-line replacement** | The real module is ToolBox's full output-path/report-writing layer (817 lines, pandas-heavy). `manifest_lookup.py` only needs one path-resolution helper. That one import site is unguarded (no `try`/`except`), so the stub must succeed on its own rather than merely existing as a fallback target — it does, unconditionally returning a valid `Path` pointing at `ToolboxStudio/backend/data/`. |
| `app.shared_excel.sharepoint_storage` | 1479 | `is_configured`, `station_cache_dir`, `push_file`, `pull_folder`, `download_manifest`, `upload_manifest`, `prune_old_manifests`, `list_manifests`, `pull_cache_path` — used by `horizon_metrics_store.py`, `manifest_lookup.py`, `rts_persistence.py` | **Vendor a slim local stub** | Most call sites are gated on `is_configured()`, but not all (two call sites in `horizon_metrics_store.py` call `station_cache_dir`/`pull_folder` unconditionally). The stub is designed to behave safely either way: `is_configured()` always returns `False` by default, `station_cache_dir()` always returns a valid local directory (never raising), and `pull_folder()` always returns `[]` (falling through to the caller's own local glob fallback) — behaviorally identical to the real module running on a station that hasn't configured SharePoint yet, whether or not a given call site happens to be gated. |
| `app.shared_excel.schemas` | 1621 | `static_fields()`, used only by `mercury_catalog.py`'s two helper functions | **Stub / do not vendor** | ToolBox's entire multi-station config-wizard schema (1621 lines). Both call sites wrap the import in `try/except Exception: return {}`. Without it, Mercury query resolution falls back to `mercury_queries.json` and plain env defaults — the correct, intended fallback path; Studio has no config wizard. |
| `app.tools.common.horarios` (3 helpers) | n/a | one deferred, explicitly-commented "lazy import, circular for its callers" call inside `manifest_lookup.load_manifest_route_dsp_context` | **Stub / do not vendor** | A ToolBox *tool*, not an integration client — out of scope (Studio vendors integrations and shared UI, not ToolBox's tool business logic). The call is wrapped in `try/except Exception`, falling back to an empty DSP map; the caller already checks for `None` before using the result — graceful degradation, not a crash. |

## `globalConnections.js` is a rewrite, not a re-sync candidate

Unlike every other vendored frontend/backend file, `frontend/src/shared/
stores/globalConnections.js` is **not** a trimmed copy of the real
Toolbox file. The real file is ~2,000 lines of cross-tab leader-election
(`BroadcastChannel`/`localStorage`) and a generation-token background
work-tab runner, built for Toolbox's "many tabs, one backend, many
coordinated login flows" situation. Studio is sized for one developer in
one Studio tab, so that machinery was stripped entirely; only the real
file's public API (`refreshOne`/`refreshAll`/`queueIdentify`/
`reconnectOne`/`reconnectIdsFromGesture`/`invalidate`/`state`) was kept,
because `useIdentity.js` calls exactly those functions and nothing else.

**Re-syncing this file later does not mean diffing and re-copying.** It
means re-reading Toolbox's then-current real file and re-deciding what
(if anything) of its evolution is worth porting into Studio's small
rewrite. The reasoning above (and the fuller version in design.md
§1.2/§1.6/§2.3) is the authoritative source for that decision — copy it
forward when re-deciding, don't re-derive it from scratch.

One accepted consequence of the rewrite: a connection that goes stale
while a Studio developer is away from the keyboard will sit disconnected
until the developer clicks a "Conectar"/"Identificar" button themselves
(a real user gesture always works; Studio's rewrite has no self-closing
background work tab to route a gesture-less reconnect through, unlike
Toolbox's real file). This is a deliberate simplification, not an
oversight — see design.md §9, scope cuts #11/#12.
