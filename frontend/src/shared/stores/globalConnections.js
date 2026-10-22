// ToolboxStudio/frontend/src/shared/stores/globalConnections.js
//
// Slimmed rewrite of ToolBox's globalConnections.js. The real file
// (2029 lines) adds a cross-tab leader-election system and a shared
// work-tab login runner on top of a small probe registry; Studio keeps
// ONLY the probe registry — the exact five functions useIdentity.js
// calls — because Studio has no multi-tab-leader or shared-login-tab
// concern. See design.md §2.3 for the full reasoning and what was cut.
import { reactive } from "vue";
import { openLoginTab } from "../openLoginTab";
import { connectionStatus } from "../api/http";

// One row per generic connection Studio's own backend can probe.
// Add an entry here + a matching case in the backend's
// app/api/connections_routes.py to register a new one.
const DEFS = [
  { id: "scc", label: "SCC", loginUrl: "https://logistics.amazon.co.uk/station/dashboard/ageing" },
  { id: "mercury", label: "Mercury", loginUrl: "https://mercury.amazon.com/" },
  { id: "quicksight", label: "QuickSight", loginUrl: "https://us-east-1.quicksight.aws.amazon.com/" },
  { id: "perfectmile", label: "PerfectMile", loginUrl: "https://perfectmile.a2z.com/" },
];

const state = reactive({});
for (const def of DEFS) {
  state[def.id] = { label: def.label, loginUrl: def.loginUrl, status: "unknown", loading: false };
}

const _openTabs = {};
const _refreshPromises = {};

async function _refreshOne(id) {
  const def = DEFS.find((d) => d.id === id);
  const row = state[id];
  if (!def || !row) return false;
  row.loading = true;
  try {
    const res = await connectionStatus(id).catch(() => null);
    const ok = Boolean(res?.connected);
    if (res?.login_url) row.loginUrl = res.login_url;
    row.status = ok ? "connected" : "disconnected";
    if (ok) _closeTab(id);
    return ok;
  } finally {
    row.loading = false;
  }
}

function refreshOne(id) {
  if (_refreshPromises[id]) return _refreshPromises[id];
  const p = _refreshOne(id).finally(() => { delete _refreshPromises[id]; });
  _refreshPromises[id] = p;
  return p;
}

async function refreshAll() {
  for (const def of DEFS) await refreshOne(def.id);
}

function _closeTab(id) {
  const handle = _openTabs[id];
  if (!handle) return;
  try { handle.close(); } catch (_e) { /* already closed */ }
  _openTabs[id] = null;
}

// Opens the login page for one connection under a real user gesture
// (click). Studio has no shared work-tab runner: each id just opens its
// own tab and the caller's own probe loop (useIdentity's reconnect loop)
// detects the session.
function reconnectOne(id, { native = false } = {}) {
  const row = state[id];
  if (!row?.loginUrl) return;
  _openTabs[id] = openLoginTab(row.loginUrl, `studio-conn-${id}`, { forceNative: native }) || null;
}

// Studio has no shared-tab arbitration to "reserve" — each reconnect
// opens its own tab, so this is a thin pass-through kept only so
// useIdentity.js's call site keeps working unmodified.
function reconnectIdsFromGesture(ids = []) {
  for (const id of ids) reconnectOne(id, { native: false });
}

// Studio has no batching/background-tab runner: open the login
// immediately. Kept so useIdentity.js's call site keeps working
// unmodified (it calls this from a non-gesture context, e.g. boot
// auto-identify, where opening a tab may be popup-blocked — this is an
// accepted, documented limitation versus the real file's self-closing
// background work tab; see §9 scope cuts).
function queueIdentify(id) {
  reconnectOne(id, { native: true });
}

function invalidate(id) {
  const row = state[id];
  if (row) row.status = "disconnected";
  delete _refreshPromises[id];
}

export function useGlobalConnections() {
  return { state, refreshOne, refreshAll, reconnectOne, reconnectIdsFromGesture, queueIdentify, invalidate };
}
