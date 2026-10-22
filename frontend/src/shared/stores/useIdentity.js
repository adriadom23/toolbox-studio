import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref } from "vue";

import { openLoginTab } from "../openLoginTab";
import { useGlobalConnections } from "./globalConnections";

/** Map per-view source variants to their shared session in the navbar. */
function _globalConnectionId(id) {
  if (typeof id !== "string") return "";
  if (id === "perfectmile" || id === "perfectmile-caravan") return "perfectmile";
  if (id === "mercury" || id === "scc" || id === "cortex" || id === "relay" || id === "horizon" || id === "routing-tools") {
    return id;
  }
  if (id === "quicksight" || id.startsWith("quicksight-")) return "quicksight";
  if (id === "timehub" || id === "shifts") return "timehub";
  if (id === "rosterview") return "rosterview";
  // Cookie-minting pages (OTR Live): driven by the shared self-closing tab.
  if (id === "harmony" || id === "pod") return id;
  return "";
}

/**
 * Generic multi-source "identification" (session login) manager.
 *
 * Every ToolBox tool needs one or more external sessions (PerfectMile,
 * QuickSight, Mercury, SCC, ...) before it can pull data. Historically each
 * tool duplicated its own ``pmReady`` / ``qsReady`` refs plus a ``setInterval``
 * poll and a login-tab opener. This composable centralises all of that so a
 * tool only has to declare *which* sources it needs and render the shared
 * ``DataSourceCard`` bound to the resulting state.
 *
 * Each source is described by an adapter (see ``identitySources.js``):
 *   {
 *     id:       "perfectmile",              // unique key
 *     label:    "PerfectMile",              // display title
 *     accent:   "perfectmile",              // DataSourceCard colour
 *     loginUrl: "https://perfectmile...",   // fallback login URL
 *     check:    async () => ({ connected, loginUrl?, error? }),
 *   }
 *
 * Status per source is a 3-state machine, mirroring the SharePoint chip:
 *   - "connected"    → green  (session live)
 *   - "reconnecting" → yellow (login opened, re-probing until it comes back)
 *   - "disconnected" → red    (no session, needs the operator)
 *
 * The combined ``identify()`` action (the single "Identificar" button) only
 * acts on the sources that are NOT connected: it opens each pending login once
 * and holds them in "reconnecting" until the session is detected. Sources that
 * are already connected are left untouched — so if one of two is down, only the
 * down one is reconnected.
 */

let POLL_MS = 60000;
// While a source is disconnected we keep re-probing this often until its
// session comes back (persistent "Reconectando..." state).
let RECONNECT_RETRY_MS = 4000;
// Auto-reconnect (on view entry) gives up after this long so the panel falls
// back to red with its manual "Conectar" buttons instead of holding the
// yellow state (which disables them) forever. Manual clicks keep the
// persistent loop.
const AUTO_RECONNECT_MAX_MS = 120000;
// Shared across every view instance: re-entering tools re-probes and re-polls,
// but doesn't re-open a login tab for the same session more than once per
// cooldown window (the silent loop still detects the login the moment the
// operator completes it elsewhere).
const AUTO_LOGIN_COOLDOWN_MS = 90000;
const _autoLoginAt = {}; // global session id (or source id) -> last auto-open ts

// One stable poll per shared global connection, regardless of how many kept-
// alive tools consume it. Subscribers only mirror the canonical result into
// their tool-specific refs; they never issue their own network probe.
const _sharedPolls = new Map();

function _subscribeSharedPoll(id, poll, listener) {
  let entry = _sharedPolls.get(id);
  if (!entry) {
    entry = {
      listeners: new Set(),
      promise: null,
      timer: null,
      run: null,
    };
    entry.run = async () => {
      if (entry.promise) return entry.promise;
      entry.promise = (async () => {
        await poll();
        await Promise.allSettled([...entry.listeners].map((notify) => notify()));
      })();
      try {
        await entry.promise;
      } finally {
        entry.promise = null;
      }
    };
    entry.timer = window.setInterval(entry.run, POLL_MS);
    _sharedPolls.set(id, entry);
  }
  entry.listeners.add(listener);
  return {
    refresh: entry.run,
    unsubscribe() {
      entry.listeners.delete(listener);
      if (entry.listeners.size) return;
      if (entry.timer) window.clearInterval(entry.timer);
      _sharedPolls.delete(id);
    },
  };
}

export function useIdentity(sources = [], { autoIdentifyOnMount = true } = {}) {
  const list = (Array.isArray(sources) ? sources : [sources]).filter(Boolean);
  const globalConnections = useGlobalConnections();

  // Per-source reactive status. ``checking`` is a transient flag while a probe
  // is in flight (so the card can show "Comprobando…" without losing the last
  // known status).
  const state = reactive({});
  for (const src of list) {
    state[src.id] = {
      status: "disconnected", // connected | reconnecting | disconnected
      error: "",
      loginUrl: src.loginUrl || "",
      checking: false,
    };
  }

  const reconnectTimers = {}; // id -> setTimeout handle
  const reconnectLoops = {}; // id -> bool (loop active)
  const loginTabs = {}; // id -> Window handle (so we can auto-close on success)
  const failStreaks = {}; // id -> consecutive failed probes (flicker guard)
  const confirmTimers = {}; // id -> quick re-probe scheduled to confirm a drop
  const sharedSubscriptions = {}; // id -> shared global poll subscription
  let pollTimer = null;

  function _src(id) {
    return list.find((s) => s.id === id) || null;
  }

  // A source that was green a minute ago often fails ONE background probe
  // transiently (backend busy mid-extraction, network flake). Downgrading on
  // that single miss flips ``allConnected`` and the tools' connection folds
  // force-open/close around it, so the whole page shifts on its own every
  // so often (reported on Forecast, 2026-08-20). Confirm before
  // downgrading: keep "connected", re-probe shortly, and only a second
  // consecutive failure drops the chip. Sources that were already red keep
  // the immediate behaviour (their state can't cause a layout flap).
  function _markDown(src) {
    const st = state[src.id];
    failStreaks[src.id] = (failStreaks[src.id] || 0) + 1;
    if (st.status === "connected" && failStreaks[src.id] < 2) {
      if (!confirmTimers[src.id]) {
        confirmTimers[src.id] = window.setTimeout(() => {
          confirmTimers[src.id] = null;
          const shared = sharedSubscriptions[src.id];
          if (shared) shared.refresh();
          else _probe(src);
        }, RECONNECT_RETRY_MS);
      }
      return;
    }
    st.status = "disconnected";
  }

  async function _syncFromShared(src) {
    const globalId = _globalConnectionId(src.id);
    const shared = globalId ? globalConnections.state[globalId] : null;
    const st = state[src.id];
    const connected = shared?.status === "connected";

    if (shared?.loginUrl) st.loginUrl = shared.loginUrl;

    if (connected) {
      if (typeof src.syncShared === "function") {
        try {
          await src.syncShared(true, shared);
        } catch (error) {
          st.error = error?.message || String(error);
        }
      }
      failStreaks[src.id] = 0;
      st.error = "";
      if (confirmTimers[src.id]) {
        window.clearTimeout(confirmTimers[src.id]);
        confirmTimers[src.id] = null;
      }
      st.status = "connected";
      _closeLoginTab(src.id);
    } else if (st.status !== "reconnecting") {
      _markDown(src);
      if (st.status === "disconnected" && typeof src.syncShared === "function") {
        try {
          await src.syncShared(false, shared);
        } catch (error) {
          st.error = error?.message || String(error);
        }
      }
    }
    return connected;
  }

  // Run one status probe for a source and fold the result into ``state``.
  // Sources with ``syncShared`` use the canonical global probe directly;
  // other adapters keep their tool-specific check and global fallback.
  async function _probe(src) {
    const st = state[src.id];
    st.checking = true;
    try {
      const globalId = _globalConnectionId(src.id);
      if (globalId && typeof src.syncShared === "function") {
        await globalConnections.refreshOne(globalId);
        return await _syncFromShared(src);
      }

      let res = null;
      let localError = "";
      try {
        res = await src.check();
      } catch (error) {
        localError = error?.message || String(error);
      }

      const connected = Boolean(res && res.connected);
      if (res && res.loginUrl) st.loginUrl = res.loginUrl;
      st.error = (res && res.error) || localError;

      if (connected) {
        failStreaks[src.id] = 0;
        if (confirmTimers[src.id]) {
          window.clearTimeout(confirmTimers[src.id]);
          confirmTimers[src.id] = null;
        }
        st.status = "connected";
        // Whatever detected the session (reconnect loop, 60s poll, manual
        // refresh): the login tab we opened has served its purpose — close it
        // so it never lingers.
        _closeLoginTab(src.id);
      } else if (st.status !== "reconnecting") {
        // Don't clobber the yellow "reconnecting" state — the reconnect loop
        // owns it until it either connects or is stopped.
        _markDown(src);
      }
      return connected;
    } catch (error) {
      st.error = error?.message || String(error);
      if (st.status !== "reconnecting") _markDown(src);
      return false;
    } finally {
      st.checking = false;
    }
  }

  async function refresh(id) {
    const src = _src(id);
    return src ? _probe(src) : false;
  }

  async function refreshAll() {
    await Promise.all(list.map((s) => _probe(s)));
  }

  function _openLogin(src, { native = false } = {}) {
    const url = state[src.id].loginUrl || src.loginUrl;
    if (!url) return;
    // ``native`` (no click gesture — view-entry auto-identify): a login tab
    // spawned through the backend launcher has no window handle, so it could
    // NEVER be auto-closed and lingered after connecting. Sources that map to
    // a shared navbar session go through the shared work-tab runner (one
    // background tab that closes itself; the self-closing hub page on
    // non-Firefox hosts) instead; the probe loop here still detects the
    // session on its own.
    const globalId = _globalConnectionId(src.id);
    if (globalId) {
      if (native) globalConnections.queueIdentify(globalId);
      else globalConnections.reconnectOne(globalId);
      return;
    }
    // Keep the handle (NO ``noopener`` — that would return null) so we can
    // auto-close the tab once the session is detected. ``openLoginTab`` opens
    // in Firefox when the Toolbox is hosted in another browser (Chrome/Edge)
    // or when ``native`` is forced (multiple tabs from one click would be
    // popup-blocked), so the backend cookie reader always sees the session; in
    // that case it returns null (the tab was launched natively and can't be
    // closed from here), and the reconnect loop still stops on its own.
    loginTabs[src.id] = openLoginTab(url, `toolbox-identify-${src.id}`, { forceNative: native }) || null;
  }

  function _closeLoginTab(id) {
    const handle = loginTabs[id];
    if (!handle) return;
    try {
      handle.close();
    } catch (_e) {
      /* cross-origin / already closed — nothing to do */
    }
    loginTabs[id] = null;
  }

  // Persistent reconnect loop for a single source: open its login once, then
  // keep re-probing every RECONNECT_RETRY_MS while it stays disconnected,
  // holding the yellow state until the session is back. Never runs twice.
  // ``maxMs`` (auto-reconnect) bounds the loop so an unattended login can't
  // hold the yellow state — and the disabled buttons — forever.
  async function _reconnectLoop(src, { openLogin = true, native = false, maxMs = 0 } = {}) {
    if (reconnectLoops[src.id] || state[src.id].status === "connected") return;
    const deadline = maxMs > 0 ? Date.now() + maxMs : Infinity;
    const globalId = _globalConnectionId(src.id);
    const globalOwnsReconnect = Boolean(globalId && typeof src.connect !== "function");
    reconnectLoops[src.id] = true;
    state[src.id].status = "reconnecting";
    if (openLogin) {
      // Shared sessions have one reconnect owner: the navbar store opens the
      // login and polls its canonical endpoint. This view only mirrors that
      // state, avoiding parallel Caravan/PerfectMile status loops.
      if (globalOwnsReconnect) {
        globalConnections.reconnectOne(globalId, { native });
      } else if (typeof src.connect === "function") {
        // A source can supply its own ``connect`` (e.g. a tool that already
        // opens a login tab and polls its own ref).
        try {
          src.connect();
        } catch (_e) {
          /* non-fatal */
        }
      } else {
        _openLogin(src, { native });
      }
    }
    try {
      while (reconnectLoops[src.id] && Date.now() < deadline) {
        let ok = false;
        if (globalOwnsReconnect) {
          // Do not re-probe the same shared endpoint here. Once the canonical
          // loop reports green, run one local check to hydrate tool-specific
          // refs and close out this view's state machine.
          const sharedState = globalConnections.state[globalId];
          if (sharedState?.status === "connected") {
            ok = typeof src.syncShared === "function"
              ? await _syncFromShared(src)
              : await _probe(src);
          } else if (sharedState?.status === "disconnected" && !sharedState?.loading) {
            // The global row may still read "disconnected" from an earlier
            // attempt while the ``reconnectOne`` this very loop just fired
            // is still converging it in the background. One last sync gives
            // that in-flight attempt a chance to land before giving up.
            ok = typeof src.syncShared === "function" ? await _syncFromShared(src) : false;
            if (!ok) break;
          }
        } else {
          ok = await _probe(src);
        }
        if (ok) {
          _closeLoginTab(src.id);
          break;
        }
        await new Promise((resolve) => {
          reconnectTimers[src.id] = window.setTimeout(resolve, RECONNECT_RETRY_MS);
        });
      }
    } finally {
      reconnectLoops[src.id] = false;
      if (state[src.id].status === "reconnecting") state[src.id].status = "disconnected";
      if (reconnectTimers[src.id]) {
        window.clearTimeout(reconnectTimers[src.id]);
        reconnectTimers[src.id] = null;
      }
    }
  }

  // True from the moment "Identificar" is pressed until its probes finish and
  // the reconnect loops are launched. It feeds ``anyReconnecting`` so the
  // button greys out INSTANTLY on press instead of waiting for the first
  // probe round-trip (which made it look like the click did nothing).
  const identifying = ref(false);

  /**
   * The combined "Identificar" action. Re-probes every source first (so we
   * never open a login tab for a session that is actually fine), then starts a
   * reconnect loop only for the sources that are still not connected.
   */
  async function identify() {
    if (identifying.value) return;
    identifying.value = true;
    // Capture the pending scope at click time. Connected sources are left
    // untouched, and shared sessions reserve the one self-closing work tab
    // synchronously before any status request can consume the popup gesture.
    const pending = list.filter((src) => state[src.id].status !== "connected");
    const driven = new Set();
    const sharedIds = [];
    for (const src of pending) {
      const globalId = _globalConnectionId(src.id);
      if (!globalId || typeof src.connect === "function") continue;
      if (globalConnections.state[globalId]?.status !== "connected" && !sharedIds.includes(globalId)) {
        sharedIds.push(globalId);
      }
      driven.add(src.id);
    }
    if (sharedIds.length) globalConnections.reconnectIdsFromGesture(sharedIds);
    try {
      // Probe only the sources that were pending when the action started.
      // Shared sessions already have a canonical runner: mirror its state
      // without issuing another status request from the tool.
      await Promise.all(pending.map((src) => (
        driven.has(src.id) ? _syncFromShared(src) : _probe(src)
      )));
      let usedGesture = sharedIds.length > 0;
      for (const src of pending) {
        if (state[src.id].status === "connected") continue;
        if (driven.has(src.id)) {
          _reconnectLoop(src, { openLogin: false });
          continue;
        }
        _reconnectLoop(src, { openLogin: true, native: usedGesture });
        usedGesture = true;
      }
    } finally {
      identifying.value = false;
    }
  }

  /** Reconnect a single source (per-card button or async auth failure). */
  function reconnect(id, { native = false } = {}) {
    const src = _src(id);
    if (!src) return;
    // A loop is already polling this source (e.g. auto-identify on view
    // entry). Don't swallow the click: its login tab may have been
    // popup-blocked, so re-open it under the fresh user gesture. Async callers
    // pass native=true and are routed through the self-closing work runner.
    if (reconnectLoops[src.id]) {
      if (typeof src.connect === "function") {
        try {
          src.connect();
        } catch (_e) {
          /* non-fatal */
        }
      } else {
        _openLogin(src, { native });
      }
      return;
    }
    _reconnectLoop(src, { openLogin: true, native });
  }

  function stopReconnect(id) {
    const ids = id ? [id] : list.map((s) => s.id);
    for (const key of ids) {
      reconnectLoops[key] = false;
      if (reconnectTimers[key]) {
        window.clearTimeout(reconnectTimers[key]);
        reconnectTimers[key] = null;
      }
    }
  }

  /**
   * Automatic identification on view entry: probe every source first, then
   * start a reconnect loop for whatever is still down — the same thing the
   * "Identificar" button does, minus the click. Entering a view is NOT a user
   * gesture, so login tabs are routed through the native (backend Firefox)
   * launcher; a plain ``window.open`` here would be silently popup-blocked.
   * The shared cooldown stops re-entries from re-spawning a login tab for a
   * session that was just offered one — the loop still runs silently and
   * picks the session up the moment the operator completes the login. The
   * loop is bounded (AUTO_RECONNECT_MAX_MS) so an ignored login drops the
   * chip back to red with its manual "Conectar" button.
   */
  async function autoIdentify() {
    await refreshAll();
    for (const src of list) {
      if (state[src.id].status === "connected") continue;
      const globalId = _globalConnectionId(src.id);
      const key = globalId || src.id;
      const now = Date.now();
      const openLogin = now - (_autoLoginAt[key] || 0) > AUTO_LOGIN_COOLDOWN_MS;
      if (openLogin) _autoLoginAt[key] = now;
      // View entry is not a user gesture. Only canonical global sessions may
      // start the self-closing invisible runner; custom connect callbacks can
      // extract data or open raw tabs and are reserved for explicit clicks.
      if (globalId && typeof src.connect !== "function" && openLogin) {
        globalConnections.reconnectOne(globalId, { native: true });
      }
      _reconnectLoop(src, {
        openLogin: false,
        native: true,
        maxMs: AUTO_RECONNECT_MAX_MS,
      });
    }
  }

  function start() {
    if (pollTimer || Object.keys(sharedSubscriptions).length) return;
    // Some read-only tools must report a disconnected source without opening
    // a login tab in the background. They still run the same pure probe and
    // retain explicit Connect/Reconnect actions in IdentifyPanel.
    if (autoIdentifyOnMount) autoIdentify();
    else refreshAll();

    const localSources = [];
    for (const src of list) {
      const globalId = _globalConnectionId(src.id);
      if (globalId && typeof src.syncShared === "function") {
        sharedSubscriptions[src.id] = _subscribeSharedPoll(
          globalId,
          () => globalConnections.refreshOne(globalId),
          () => _syncFromShared(src),
        );
      } else {
        localSources.push(src);
      }
    }

    if (localSources.length) {
      pollTimer = window.setInterval(() => {
        // Skip sources that are mid-reconnect — their own loop is polling.
        for (const src of localSources) if (!reconnectLoops[src.id]) _probe(src);
      }, POLL_MS);
    }
  }

  function stop() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    for (const key of Object.keys(sharedSubscriptions)) {
      sharedSubscriptions[key].unsubscribe();
      delete sharedSubscriptions[key];
    }
    // Drop any pending drop-confirmation probe: the view is gone, nothing
    // should keep probing (and _probe on a dead view is wasted work).
    for (const key of Object.keys(confirmTimers)) {
      if (confirmTimers[key]) {
        window.clearTimeout(confirmTimers[key]);
        confirmTimers[key] = null;
      }
    }
    stopReconnect();
    // Close any login tab we opened that is still pending: once the view is
    // gone its reconnect loop dies with it, so nothing would ever auto-close
    // the tab (it only closes on session-detected). Leaving the view mid-login
    // must not leak an orphan tab.
    for (const src of list) _closeLoginTab(src.id);
  }

  // Most tools auto-identify on entry. A read-only tool can opt into a
  // probe-only start so background polling never creates a login tab.
  onMounted(start);
  onActivated(() => (autoIdentifyOnMount ? autoIdentify() : refreshAll()));
  onBeforeUnmount(stop);

  const allConnected = computed(
    () => list.length > 0 && list.every((s) => state[s.id].status === "connected"),
  );
  const anyConnected = computed(() => list.some((s) => state[s.id].status === "connected"));
  const anyReconnecting = computed(
    () => identifying.value || list.some((s) => state[s.id].status === "reconnecting"),
  );
  const anyDisconnected = computed(() => list.some((s) => state[s.id].status === "disconnected"));
  // Number of sources still pending (not connected). No longer shown on the
  // combined button (plain "Reconectar" since 2026-08-26); kept in the API
  // for consumers that want it.
  const pendingCount = computed(
    () => list.filter((s) => state[s.id].status !== "connected").length,
  );

  return {
    state,
    sources: list,
    refresh,
    refreshAll,
    autoIdentify,
    identify,
    reconnect,
    stopReconnect,
    start,
    stop,
    allConnected,
    anyConnected,
    anyReconnecting,
    anyDisconnected,
    pendingCount,
    isConnected: (id) => state[id]?.status === "connected",
    isReconnecting: (id) => state[id]?.status === "reconnecting",
    isDisconnected: (id) => state[id]?.status === "disconnected",
    statusOf: (id) => state[id]?.status || "disconnected",
  };
}

// Narrow deterministic timing seam for the Node regression tests (mirrors
// ``__setGlobalConnectionTestTiming`` in globalConnections.js). Production
// callers never invoke this, so the user-visible cadence remains unchanged.
export function __setIdentityTestTiming({ pollMs = 60000, reconnectRetryMs = 4000 } = {}) {
  POLL_MS = pollMs;
  RECONNECT_RETRY_MS = reconnectRetryMs;
}
