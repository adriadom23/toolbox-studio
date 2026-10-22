/**
 * Reactive station configuration fetched from /api/health.
 *
 * Trimmed version of ToolBox's api/stationConfig.js (see design.md §2.5):
 * keeps the generic multisite/date-picker surface that PlanManifestPanel.vue
 * and the vendored stores need, and drops the entire operator-access-gate
 * section (operatorLogin, checkAccess, switchStation, etc.) — that section
 * only exists to back AccessGate.vue and ToolBox's shift-roster gate,
 * neither of which is vendored or has any meaning for a blank Studio
 * project.
 */
import { computed, ref, watch } from "vue";
import { getHealth } from "./http";

export const stationCodes = ref([]);
// Primary station only (the ``STATION_CODE`` env, without multisite
// extras). Some views must show a single station and always use this
// one, never the multisite list.
export const primaryStation = ref("");
// Country of the station. Drives any deep links that need it.
export const stationCountry = ref("ES");

// Studio has no "Problem Solver build" variant — this is kept as a
// correctly-typed stub (a real Vue computed ref, not a bare `false`
// primitive) because real consumers always read `.value` on it. See
// design.md §2.5 (review Finding 3): a bare `false` has no `.value`
// property and would throw the moment any such code reads it.
export const isPsProfile = computed(() => false);

export const isMultisite = computed(() => stationCodes.value.length > 1);
export const activeSite = ref(
  (typeof localStorage !== "undefined" && localStorage.getItem("toolbox.activeSite")) || "__ALL__"
);

watch(activeSite, (val) => {
  try { localStorage.setItem("toolbox.activeSite", val); } catch (_e) { /* ignore */ }
});

/** Global operational date (YYYY-MM-DD). Defaults to today (local). */
function _todayLocal() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}
export const activeDate = ref(_todayLocal());

// Persist the active operational date so non-Vue modules (e.g. the API
// layer in http.js) can read the currently selected day without an
// import cycle. Mirrors how the locale is shared.
try {
  if (typeof localStorage !== "undefined") localStorage.setItem("toolbox.activeDate", activeDate.value);
} catch (_e) { /* ignore */ }
watch(activeDate, (val) => {
  try { localStorage.setItem("toolbox.activeDate", val || _todayLocal()); } catch (_e) { /* ignore */ }
});

/** Reset activeDate to today. Called when navigating back to the project list. */
export function resetDateToToday() {
  activeDate.value = _todayLocal();
}

let _loaded = false;

export async function loadStationConfig({ force = false } = {}) {
  if (!force && _loaded && stationCodes.value.length) return;
  try {
    const health = await getHealth();
    const codes = health?.station_codes;
    primaryStation.value = String(
      health?.station_code || (Array.isArray(codes) && codes[0]) || ""
    ).trim().toUpperCase();
    stationCountry.value =
      String(health?.station_country || "").trim().toUpperCase() || "ES";
    if (Array.isArray(codes) && codes.length) {
      stationCodes.value = codes;
    } else if (health?.station_code) {
      stationCodes.value = [health.station_code];
    }
    if (stationCodes.value.length === 1) {
      activeSite.value = stationCodes.value[0];
    } else if (stationCodes.value.length > 1) {
      const stored = activeSite.value;
      const validValues = [...stationCodes.value, "__ALL__"];
      if (!validValues.includes(stored)) {
        activeSite.value = "__ALL__";
      }
    }
    _loaded = true;
  } catch (_err) {
    if (!stationCodes.value.length) {
      stationCodes.value = ["DS"];
    }
  }
}
