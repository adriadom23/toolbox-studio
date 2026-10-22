/**
 * Reactive central store that mirrors the Plan Manifest panel state.
 *
 * The panel pushes its current snapshot here on every reload / save
 * / fetch. Any other component (the per-tool ManifestField dropzones,
 * processing buttons, etc.) reads from here so it sees the same data
 * the operator just acted upon — no extra HTTP calls, no stale state.
 *
 * Plus: an `activeKey` lets the operator pick which (station, cycle)
 * pair the current tool should consume when several are saved. The
 * panel publishes every saved manifest; the per-tool ManifestField
 * paints them all and lets the user pick the active one with a click.
 *
 * Shape:
 *   manifestStore.date       -> "YYYY-MM-DD"
 *   manifestStore.byStation  -> { [STATION]: [{ cycle, path, name, ... }] }
 *   manifestStore.activeKey  -> "STATION|CYCLE" of the manifest the
 *                               consumer tool should use, or "" if
 *                               nothing has been picked yet.
 */
import { computed, reactive } from "vue";

export const manifestStore = reactive({
  date: "",
  byStation: {},
  activeKey: "",
  loading: false,
});

function _key(station, cycle) {
  return `${String(station || "").toUpperCase()}|${String(cycle || "").toUpperCase()}`;
}

/**
 * Track whether the Plan Manifest panel is currently fetching its
 * snapshot from the shared drive. Consumer tools (e.g. Cierre) can wait
 * on this before processing so the auto-resolved manifest is available
 * on the first click instead of forcing the operator to re-click.
 */
export function setLoading(value) {
  manifestStore.loading = Boolean(value);
}

export function setSnapshot({ date, manifests } = {}) {
  manifestStore.date = String(date || "");
  const next = {};
  for (const m of manifests || []) {
    const code = String(m.stationCode || m.station_code || "").toUpperCase();
    if (!code) continue;
    if (!next[code]) next[code] = [];
    next[code].push({
      cycle: String(m.cycle || "").toUpperCase(),
      path: String(m.path || ""),
      name: String(m.name || ""),
      size: Number(m.size || 0),
      modified_at: Number(m.modified_at || 0),
    });
  }
  for (const code of Object.keys(next)) {
    next[code].sort((a, b) => (b.modified_at || 0) - (a.modified_at || 0));
  }
  for (const k of Object.keys(manifestStore.byStation)) {
    delete manifestStore.byStation[k];
  }
  for (const [k, v] of Object.entries(next)) {
    manifestStore.byStation[k] = v;
  }
  // Keep activeKey valid: drop it if it points to a manifest that no
  // longer exists; auto-pick the first one when nothing is selected.
  const everyKey = [];
  for (const [code, list] of Object.entries(next)) {
    for (const m of list) everyKey.push(_key(code, m.cycle));
  }
  if (manifestStore.activeKey && !everyKey.includes(manifestStore.activeKey)) {
    manifestStore.activeKey = "";
  }
  if (!manifestStore.activeKey && everyKey.length === 1) {
    manifestStore.activeKey = everyKey[0];
  }
}

export function setActive(station, cycle) {
  manifestStore.activeKey = _key(station, cycle);
}

export function clearActive() {
  manifestStore.activeKey = "";
}

export function manifestsFor(station) {
  const code = String(station || "").toUpperCase();
  return manifestStore.byStation[code] || [];
}

export function firstFor(station) {
  const list = manifestsFor(station);
  return list.length ? list[0] : null;
}

export function hasAny(station) {
  return manifestsFor(station).length > 0;
}

/** Flatten every saved manifest into a single ordered list. */
export function allManifests() {
  const out = [];
  for (const [station, list] of Object.entries(manifestStore.byStation)) {
    for (const m of list) {
      out.push({ ...m, station });
    }
  }
  // Sort by station then by saved time desc.
  out.sort((a, b) => {
    if (a.station !== b.station) return a.station.localeCompare(b.station);
    return (b.modified_at || 0) - (a.modified_at || 0);
  });
  return out;
}

/** Active manifest object, or null if none picked / available. */
export function activeManifest() {
  if (!manifestStore.activeKey) return null;
  const [station, cycle] = manifestStore.activeKey.split("|");
  const list = manifestsFor(station);
  return list.find((m) => (m.cycle || "").toUpperCase() === cycle) || null;
}

/** Reactive computed helper for components: returns activeManifest(). */
export const activeManifestRef = computed(() => activeManifest());

export function isActive(station, cycle) {
  return manifestStore.activeKey === _key(station, cycle);
}
