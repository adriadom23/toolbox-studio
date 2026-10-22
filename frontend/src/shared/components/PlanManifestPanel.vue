<template>
  <section class="pmp-wrap">
    <!-- The whole extraction panel folds into a single bar. It stays
         OPEN until every needed source (manifest + extras) is extracted,
         also while something is being recovered (or an error must be
         seen), and folds as soon as the extraction is complete. -->
    <CollapsedSection
      :label="t('planManifest.title')"
      :force-open="panelForceOpen"
    >
      <template #summary>
        <span
          class="pmp-connection-state"
          :class="`pmp-connection-state--${routingToolsState}`"
        >
          ● {{ routingToolsStateLabel }}
        </span>
      </template>
      <!-- Mini action on the folded bar (same pattern as the other
           connection sections): when RoutingTools is disconnected, a
           compact icon button triggers the login + recover-all flow
           without having to expand the panel first. While the login
           polling / recovery runs, panelForceOpen opens the panel so
           the progress stays visible. -->
      <template #actions>
        <button
          v-if="showManifest && routingToolsState === 'disconnected'"
          type="button"
          class="tb-button tb-button--primary pmp-reconnect-mini"
          :title="t('planManifest.actionLoginAndFetch')"
          :aria-label="t('planManifest.actionLoginAndFetch')"
          @click="onTopAction"
        >
          <span aria-hidden="true">🔑</span>
        </button>
      </template>
    <section
      class="plan-manifest-panel"
      :class="{ 'is-loading': loading }"
    >
      <!-- ─── Header ─────────────────────────────────────── -->
      <header class="pmp-head">
        <div class="pmp-head__main">
          <span class="badge badge--manifest">
            🧰 Routing Tools
            <span
              class="pmp-connection-state"
              :class="`pmp-connection-state--${routingToolsState}`"
            >
              ● {{ routingToolsStateLabel }}
            </span>
          </span>
          <h3>{{ t("planManifest.title") }}</h3>
          <p class="hint">{{ t("planManifest.hint") }}</p>
        </div>
        <div class="pmp-head__controls">
          <button
            type="button"
            class="tb-button tb-button--ghost"
            :disabled="loading"
            @click="reload"
          >
            {{ t("planManifest.reload") }}
          </button>
        </div>
      </header>

      <p v-if="loadError" class="error-text">{{ loadError }}</p>

      <!-- ─── Shared top action: identification + recover all ──── -->
      <!-- A single, common control for every box. RoutingTools login and
           "Recuperar archivos" live here and apply to the manifest AND all
           extra sources at once. Recovering is blocked until a cycle is
           selected. -->
      <div
        v-if="topAction.show"
        class="pmp-actions-strip"
        :class="{ 'pmp-actions-strip--auth': topAction.auth }"
      >
        <p class="pmp-actions-strip__msg">{{ topAction.msg }}</p>
        <span v-if="topAction.busy" class="pmp-actions-strip__busy">
          {{ t("planManifest.fetchingShort") }}
        </span>
        <button
          v-else
          type="button"
          class="tb-button tb-button--primary"
          :disabled="topAction.disabled"
          @click="onTopAction"
        >
          {{ topAction.label }}
        </button>
      </div>

      <p v-if="!loading && !filteredStations.length" class="hint">
        {{ t("planManifest.noStations") }}
      </p>

      <!-- ─── Cycle selector per station ─────────────────── -->
      <div class="pmp-stations">
        <section
          v-for="station in filteredStations"
          :key="station.code"
          class="pmp-station"
        >
          <header class="pmp-station__head">
            <h4>{{ station.code }}</h4>
            <span
              class="pmp-pill"
              :class="station.any_found ? 'pmp-pill--ok' : 'pmp-pill--miss'"
            >
              {{
                station.any_found
                  ? t("planManifest.station.ready", { count: station.manifests.length })
                  : t("planManifest.station.missing")
              }}
            </span>
          </header>

          <div class="pmp-cycle-grid">
            <button
              v-for="cycle in cycles"
              :key="`${station.code}-${cycle}`"
              type="button"
              class="pmp-chip"
              :class="{
                'pmp-chip--saved': isSaved(station.code, cycle) && !isDeselected(station.code, cycle),
                'pmp-chip--off': isSaved(station.code, cycle) && isDeselected(station.code, cycle),
                'pmp-chip--selected': !isSaved(station.code, cycle) && isSelected(station.code, cycle),
                'is-busy': isBusyCycle(station.code, cycle),
              }"
              :disabled="busyStation === station.code"
              :title="chipTooltip(station.code, cycle)"
              @click="toggleCycle(station.code, cycle)"
              @dblclick="onCycleDblClick(station.code, cycle)"
            >
              <span
                v-if="isSaved(station.code, cycle) && !isDeselected(station.code, cycle)"
                class="pmp-chip__check"
              >✓</span>
              <span
                v-else-if="isSaved(station.code, cycle) && isDeselected(station.code, cycle)"
                class="pmp-chip__check"
              >✕</span>
              <span v-else-if="isSelected(station.code, cycle)" class="pmp-chip__check">○</span>
              <span class="pmp-chip__name">{{ cycle }}</span>
            </button>
          </div>
        </section>
      </div>

      <!-- ─── Source boxes: Plan Manifest + extra sources ──── -->
      <!-- Each data source (Plan Manifest, DA Assignment, Route Sheets)
           gets its own box. All boxes share the same per-DS/per-cycle
           grid driven by the chip selection above. Recovery + login are
           handled by the shared top action strip. -->
      <section v-if="filteredStations.length" class="pmp-boxes">
        <article
          v-for="box in boxes"
          :key="box.key"
          class="pmp-box"
          :class="{ 'pmp-box--src': box.kind !== 'manifest' }"
        >
          <header class="pmp-box__head">
            <div class="pmp-box__title">
              <h4>{{ box.titleKey ? t(box.titleKey) : box.title }}</h4>
              <span class="pmp-box__counter">
                {{
                  t("planManifest.slotsCounter", {
                    saved: boxCounts(box).saved,
                    pending: boxCounts(box).pending,
                  })
                }}
              </span>
            </div>
          </header>

          <!-- No cycle selected: block recovery, explain why. -->
          <p v-if="!cycleContextReady" class="pmp-box__empty">
            {{ t("planManifest.actionSelectCycle") }}
          </p>

          <div v-else class="pmp-slot-grid">
            <!-- Plan Manifest slots (XLSX persisted on the shared drive). -->
            <template v-if="box.kind === 'manifest'">
              <article
                v-for="slot in slotsForBox(box)"
                :key="`m-${slot.stationCode}-${slot.cycle}`"
                class="pmp-slot"
                :class="{
                  'pmp-slot--ok': slot.saved && !slot.invalid,
                  'pmp-slot--invalid': slot.saved && slot.invalid,
                  'pmp-slot--pending': !slot.saved,
                  'is-dragover': dragoverSlotKey === `${slot.stationCode}-${slot.cycle}`,
                  'is-busy': isBusyCycle(slot.stationCode, slot.cycle),
                }"
                @dragover.prevent="dragoverSlotKey = `${slot.stationCode}-${slot.cycle}`"
                @dragleave.prevent="dragoverSlotKey = ''"
                @drop.prevent="(ev) => onSlotDrop(slot, ev)"
              >
                <header class="pmp-slot__head">
                  <div>
                    <span class="pmp-slot__station">{{ slot.stationCode }}</span>
                    <span class="pmp-slot__cycle">{{ slot.cycle }}</span>
                  </div>
                  <span
                    class="pmp-slot__status"
                    :class="slot.saved
                      ? (slot.invalid ? 'pmp-slot__status--invalid' : 'pmp-slot__status--ok')
                      : 'pmp-slot__status--pending'"
                  >
                    {{
                      slot.saved
                        ? (slot.invalid ? t("planManifest.slotInvalid") : t("planManifest.slotSaved"))
                        : t("planManifest.slotPending")
                    }}
                  </span>
                </header>

                <div v-if="slot.saved" class="pmp-slot__body">
                  <code class="pmp-slot__name" :title="slot.name">{{ slot.name }}</code>
                  <p class="pmp-slot__meta" :class="{ 'pmp-slot__meta--invalid': slot.invalid }">
                    {{ formatSize(slot.size) }} · {{ formatModified(slot.modified_at) }}
                  </p>
                  <p v-if="slot.invalid" class="pmp-slot__invalid-hint">
                    {{ t("planManifest.slotInvalidHint") }}
                  </p>
                </div>
                <p v-else class="pmp-slot__hint">
                  {{ t("planManifest.slotPendingHint") }}
                </p>

                <footer class="pmp-slot__actions">
                  <label class="pmp-slot__upload">
                    <input
                      type="file"
                      accept=".xlsx,.xlsm,.xls"
                      :disabled="busyStation === slot.stationCode"
                      @change="(ev) => onSlotFile(slot, ev)"
                    />
                    <span class="tb-button tb-button--ghost tb-button--sm">
                      {{ t("planManifest.slotUploadBtn") }}
                    </span>
                  </label>
                  <button
                    type="button"
                    class="tb-button tb-button--sm"
                    :class="slot.saved ? 'tb-button--ghost' : 'tb-button--primary'"
                    :disabled="busyStation === slot.stationCode"
                    @click="onSlotFetch(slot)"
                  >
                    {{
                      isBusyCycle(slot.stationCode, slot.cycle)
                        ? t("planManifest.fetchingShort")
                        : slot.saved
                          ? t("planManifest.slotReplaceBtn")
                          : t("planManifest.slotFetchBtn")
                    }}
                  </button>
                </footer>

                <p v-if="slot.error" class="pmp-slot__error error-text">
                  {{ slot.error }}
                </p>
              </article>
            </template>

            <!-- Extra source slots (DA Assignment, Route Sheets, ...). -->
            <template v-else>
              <article
                v-for="slot in slotsForBox(box)"
                :key="`s-${box.key}-${slot.stationCode}-${slot.cycle}`"
                class="pmp-slot pmp-slot--src"
                :class="{
                  'pmp-slot--ok': slot.saved,
                  'pmp-slot--pending': !slot.saved,
                  'is-busy': slot.busy,
                }"
              >
                <header class="pmp-slot__head">
                  <div>
                    <span class="pmp-slot__station">{{ slot.stationCode }}</span>
                    <span class="pmp-slot__cycle">{{ slot.cycle }}</span>
                  </div>
                  <span
                    class="pmp-slot__status"
                    :class="slot.saved ? 'pmp-slot__status--ok' : 'pmp-slot__status--pending'"
                  >
                    {{ slot.saved ? t("planManifest.slotSaved") : t("planManifest.slotPending") }}
                  </span>
                </header>

                <div v-if="slot.saved" class="pmp-slot__body pmp-src-body">
                  <span class="pmp-src-check">✓</span>
                  <div class="pmp-src-info">
                    <p v-if="slot.count > 0" class="pmp-slot__meta">{{ t("planManifest.srcRoutes", { count: slot.count }) }}</p>
                    <code v-if="slot.name" class="pmp-slot__name" :title="slot.name">{{ slot.name }}</code>
                  </div>
                </div>
                <p v-else class="pmp-slot__hint">
                  {{ t("planManifest.srcPendingHint") }}
                </p>

                <footer class="pmp-slot__actions">
                  <label v-if="box.upload" class="pmp-slot__upload">
                    <input
                      type="file"
                      :accept="box.accept || '.csv,.json'"
                      :disabled="slot.busy || anyBusy"
                      @change="(ev) => onSrcSlotFile(box, slot, ev)"
                    />
                    <span class="tb-button tb-button--ghost tb-button--sm">
                      {{ t("planManifest.srcUploadBtn") }}
                    </span>
                  </label>
                  <button
                    type="button"
                    class="tb-button tb-button--sm"
                    :class="slot.saved ? 'tb-button--ghost' : 'tb-button--primary'"
                    :disabled="slot.busy || anyBusy"
                    @click="onSrcSlotFetch(box, slot)"
                  >
                    {{
                      (slot.busy || anyBusy)
                        ? t("planManifest.fetchingShort")
                        : slot.saved
                          ? t("planManifest.slotReplaceBtn")
                          : t("planManifest.slotFetchBtn")
                    }}
                  </button>
                </footer>

                <p v-if="slot.error" class="pmp-slot__error error-text">
                  {{ slot.error }}
                </p>
              </article>
            </template>
          </div>
        </article>
      </section>

      <!-- ─── Extras / legacy ─────────────────────────────── -->
      <details v-if="extras.length || legacy.length" class="pmp-extras">
        <summary>{{ t("planManifest.extrasTitle") }}</summary>
        <section v-for="ex in extras" :key="`extra-${ex.code}`" class="pmp-station pmp-station--extra">
          <header class="pmp-station__head">
            <h4>{{ ex.code }} <span class="pmp-extra-badge">{{ t("planManifest.extraStation") }}</span></h4>
          </header>
          <ul class="pmp-list">
            <li v-for="entry in ex.manifests" :key="entry.path" class="pmp-row">
              <div class="pmp-row__main">
                <span class="pmp-row__cycle">{{ entry.cycle || "-" }}</span>
                <code class="pmp-row__name">{{ entry.name }}</code>
              </div>
              <span class="pmp-row__meta">
                {{ formatSize(entry.size) }} · {{ formatModified(entry.modified_at) }}
              </span>
            </li>
          </ul>
        </section>
        <ul v-if="legacy.length" class="pmp-list pmp-list--legacy">
          <li
            v-for="entry in legacy"
            :key="entry.path"
            class="pmp-row pmp-row--legacy"
          >
            <div class="pmp-row__main">
              <span class="pmp-row__cycle pmp-row__cycle--legacy">{{ t("planManifest.legacyTag") }}</span>
              <code class="pmp-row__name">{{ entry.name }}</code>
            </div>
            <span class="pmp-row__meta">
              {{ formatSize(entry.size) }} · {{ formatModified(entry.modified_at) }}
            </span>
          </li>
        </ul>
      </details>
    </section>
    </CollapsedSection>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";

import {
  manifestCookiesStatus,
  manifestCycles as fetchCycles,
  manifestFetch,
  manifestStatus,
  manifestUpload,
} from "../api/http";
import { activeSite, activeDate, stationCodes } from "../api/stationConfig";
import CollapsedSection from "./CollapsedSection.vue";
import { useI18n } from "../i18n";
import { setSnapshot as setManifestSnapshot, setLoading as setManifestLoading } from "../stores/manifestStore";

const props = defineProps({
  autoload: { type: Boolean, default: true },
  // Opt-in: additional fetchable sources rendered alongside the manifest
  // and included in the unified login + fetch primary action. Each item
  // is a reactive object owned by the host:
  //   { key, title, accept?, loginUrl?,
  //     fetch: async ({ stationCode, cycle, date }) =>
  //        ({ ok?, authError?, loginUrl?, message?, count?, name? }),
  //     upload?: async ({ file, stationCode, cycle, date }) => ({ ok?, ... }) }
  extraSources: { type: Array, default: () => [] },
  // Set to false when the host only needs the extra sources (e.g. a tool
  // that consumes DA Assignment + Route Sheets but not the manifest). The
  // station roster + cycle selector still drive the boxes, but the Plan
  // Manifest box and its RoutingTools login gate are hidden.
  showManifest: { type: Boolean, default: true },
});

const emit = defineEmits(["change", "ready", "saved", "selection-change"]);

const { t } = useI18n();

const loading = ref(false);
const loadError = ref("");
const stations = ref([]);
const extras = ref([]);
const legacy = ref([]);
const cycles = ref([
  "AD_HOC_1", "AD_HOC_2", "CRTN",
  "CYCLE_1", "CYCLE_2", "CYCLE_EMER",
  "CYCLE_SD_A", "CYCLE_SD_B", "CYCLE_SD_C", "CYCLE_SD_D",
  "High_Value", "REDELIVERY",
  "RTS_1", "RTS_2", "RTS_3",
  "SWA_AM", "SWA_EARLY_AM", "SWA_INJECTION_CYCLE", "SWA_LATE_PM", "SWA_PM",
  "TEST_1",
]);
const defaultCycle = ref("CYCLE_1");

const selections = reactive({});
// Saved (already downloaded) cycles the operator has explicitly UNMARKED.
// A downloaded file whose cycle lands here is hidden from the boxes and
// excluded from the snapshot consumed by every downstream tool, so an
// unticked type never loads even though its file exists on the drive.
const deselected = reactive({});
const slotErrors = reactive({});
const busyStation = ref("");
const busyAction = ref("");
const busyCycle = ref("");
const dragoverSlotKey = ref("");
const cookiesInfo = ref({ ready: false, login_url: "" });
const cookiesChecking = ref(true);
const loginPolling = ref(false);
const routingToolsState = computed(() => {
  if (cookiesChecking.value || loginPolling.value) return "checking";
  return cookiesInfo.value.ready ? "connected" : "disconnected";
});
const routingToolsStateLabel = computed(() => t(`common.${routingToolsState.value}`));
let loginPollingTimer = null;
let loginPollingTab = null;
let postLoginAction = null;

function _isoDateMinus(days) {
  // Build a YYYY-MM-DD string in LOCAL timezone (not UTC). The backend
  // stores files under the operator's local date, so using
  // toISOString() would silently shift the date by 1 in any TZ where
  // local midnight maps to "yesterday 22:00" UTC (which is most of EU).
  const d = new Date();
  d.setDate(d.getDate() - Math.max(0, Number(days) || 0));
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

// Date: read from global navbar.
const selectedDate = computed(() => activeDate.value || _isoDateMinus(0));

// ─── Site filter ──────────────────────────────────────────
// Read from the global activeSite (controlled by the navbar).
const panelSite = computed(() => activeSite.value || "__ALL__");

const filteredStations = computed(() => {
  if (!panelSite.value || panelSite.value === "__ALL__") return stations.value;
  return stations.value.filter((s) => s.code === panelSite.value);
});

/** Preferred station code for links — first from the filter. */
const linkSite = computed(() => {
  if (panelSite.value && panelSite.value !== "__ALL__") return panelSite.value;
  return stationCodes.value?.[0] || stations.value?.[0]?.code || "";
});

// ─── Selection helpers ─────────────────────────────────────
function _ensureSelection(stationCode) {
  if (!selections[stationCode]) selections[stationCode] = new Set();
  return selections[stationCode];
}

function isSelected(stationCode, cycle) {
  return selections[stationCode]?.has(cycle) || false;
}

function _ensureDeselected(stationCode) {
  if (!deselected[stationCode]) deselected[stationCode] = new Set();
  return deselected[stationCode];
}

// A downloaded file the operator has turned off. While deselected the
// file stays on the drive but is treated as "not loaded" everywhere.
function isDeselected(stationCode, cycle) {
  return deselected[stationCode]?.has(cycle) || false;
}

function selectedCycles(stationCode) {
  return Array.from(selections[stationCode] || []);
}

function isSaved(stationCode, cycle) {
  return Boolean(metaFor(stationCode, cycle));
}

function metaFor(stationCode, cycle) {
  const station = stations.value.find((s) => s.code === stationCode);
  if (!station) return null;
  return (
    station.manifests.find((m) => (m.cycle || "").toUpperCase() === cycle) ||
    null
  );
}

function isBusyCycle(stationCode, cycle) {
  return (
    busyStation.value === stationCode &&
    busyAction.value === "fetch" &&
    busyCycle.value === cycle
  );
}

function chipTooltip(stationCode, cycle) {
  const saved = metaFor(stationCode, cycle);
  if (saved) {
    if (isDeselected(stationCode, cycle)) {
      return `${cycle} · ${t("planManifest.tooltipDeselected")}`;
    }
    const ts = formatModified(saved.modified_at);
    return ts ? `${cycle} · ${ts}` : cycle;
  }
  if (isSelected(stationCode, cycle)) {
    return `${cycle} · ${t("planManifest.tooltipSelected")}`;
  }
  return `${cycle} · ${t("planManifest.tooltipMissing")}`;
}

function toggleCycle(stationCode, cycle) {
  if (isSaved(stationCode, cycle)) {
    // Saved file: toggle whether it loads. Unmarking keeps the file on
    // the drive but excludes it from the boxes, the snapshot, and every
    // downstream tool.
    const d = _ensureDeselected(stationCode);
    if (d.has(cycle)) d.delete(cycle);
    else d.add(cycle);
  } else {
    // Not yet downloaded: toggle whether we intend to recover it.
    const s = _ensureSelection(stationCode);
    if (s.has(cycle)) s.delete(cycle);
    else s.add(cycle);
  }
  emitSelectionChange();
}

function onCycleDblClick(stationCode, cycle) {
  _ensureSelection(stationCode).add(cycle);
  // Re-fetching a previously unmarked file re-marks it.
  deselected[stationCode]?.delete(cycle);
  fetchOne({ stationCode, cycle });
}

// ─── Slot derivation ───────────────────────────────────────
const visibleSlots = computed(() => {
  const out = [];
  const seen = new Set();
  for (const station of filteredStations.value) {
    for (const m of station.manifests) {
      const cycle = (m.cycle || "").toUpperCase();
      // Unmarked saved files don't load → keep them out of the boxes.
      if (isDeselected(station.code, cycle)) continue;
      const key = `${station.code}-${cycle}`;
      seen.add(key);
      out.push({
        stationCode: station.code,
        cycle,
        saved: true,
        path: m.path,
        name: m.name,
        size: m.size,
        modified_at: m.modified_at,
        invalid: (Number(m.size) || 0) === 0,
        error: slotErrors[key] || "",
      });
    }
    for (const cycle of selectedCycles(station.code)) {
      const key = `${station.code}-${cycle}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        stationCode: station.code,
        cycle,
        saved: false,
        error: slotErrors[key] || "",
      });
    }
  }
  out.sort((a, b) => {
    if (a.stationCode !== b.stationCode) return a.stationCode.localeCompare(b.stationCode);
    if (a.saved !== b.saved) return a.saved ? -1 : 1;
    return a.cycle.localeCompare(b.cycle);
  });
  return out;
});

const pendingSlots = computed(() => visibleSlots.value.filter((s) => !s.saved));

// ─── Source boxes (Plan Manifest + extra sources) ────────
// A cycle must be selected (or already saved) before we let the operator
// recover files. visibleSlots holds both saved + selected cycles, so an
// empty list means nothing has been picked yet.
const cycleContextReady = computed(() => visibleSlots.value.length > 0);

// One box per data source. The manifest box is always present; every
// entry in the `extraSources` prop (DA Assignment, Route Sheets, ...)
// becomes its own box that mirrors the manifest's per-DS/per-cycle grid.
const boxes = computed(() => {
  const list = [];
  if (props.showManifest) {
    list.push({ key: "__manifest__", title: t("planManifest.boxManifest"), kind: "manifest" });
  }
  for (const src of props.extraSources || []) {
    list.push({ ...src, kind: "extra" });
  }
  return list;
});

// Per-extra-source slot state, keyed by `${srcKey}` → `${station}-${cycle}`.
// Tracks whether each (DS, cycle) tuple has been fetched/uploaded so the
// box can render the same saved/pending cards as the manifest.
const srcSlotState = reactive({});

function _srcSlot(srcKey, slotKey) {
  if (!srcSlotState[srcKey]) srcSlotState[srcKey] = {};
  if (!srcSlotState[srcKey][slotKey]) {
    srcSlotState[srcKey][slotKey] = {
      saved: false,
      count: 0,
      name: "",
      error: "",
      busy: false,
    };
  }
  return srcSlotState[srcKey][slotKey];
}

// Busy flag for the per-box "Recuperar archivos" loop (extra sources).
const boxBusy = reactive({});

// Slots for a given box. The manifest box reuses visibleSlots; extra
// source boxes mirror the very same (DS, cycle) combinations so all
// boxes stay in lock-step with the chip selection.
function slotsForBox(box) {
  if (box.kind === "manifest") return visibleSlots.value;
  const state = srcSlotState[box.key] || {};
  return visibleSlots.value.map((s) => {
    const slotKey = `${s.stationCode}-${s.cycle}`;
    const meta = state[slotKey] || {};
    return {
      stationCode: s.stationCode,
      cycle: s.cycle,
      saved: Boolean(meta.saved),
      count: meta.count || 0,
      name: meta.name || "",
      error: meta.error || "",
      busy: Boolean(meta.busy),
    };
  });
}

function boxCounts(box) {
  const slots = slotsForBox(box);
  const saved = slots.filter((s) => s.saved).length;
  return { saved, pending: slots.length - saved, total: slots.length };
}

// ─── Shared top action (identification + recover all) ────
// Total pending slots across the manifest box and every extra source.
const totalPending = computed(() => {
  let n = props.showManifest ? pendingSlots.value.length : 0;
  for (const box of boxes.value) {
    if (box.kind === "manifest") continue;
    n += slotsForBox(box).filter((s) => !s.saved).length;
  }
  return n;
});

// Any in-flight recovery, anywhere (manifest or extra source).
const anyBusy = computed(() => {
  if (busyAction.value === "fetch") return true;
  if (Object.keys(boxBusy).some((k) => boxBusy[k])) return true;
  for (const key of Object.keys(srcSlotState)) {
    const state = srcSlotState[key] || {};
    if (Object.values(state).some((s) => s.busy)) return true;
  }
  return false;
});

// Everything the tool needs has been recovered: at least one slot is
// visible AND none is still pending, across the manifest box and every
// extra source (DA Assignment, Route Sheets, ...).
const allExtracted = computed(
  () => cycleContextReady.value && totalPending.value === 0,
);

// Keeps the folded panel forced open while the extraction is incomplete
// (user rule 2026-08-19: the panel must NOT appear minimized until the
// sources are extracted), while something is actively happening
// (recovery in flight, login polling) or an error needs to be seen.
// When it turns false again the CollapsedSection folds itself, so the
// panel is minimized as soon as the extraction ends.
const panelForceOpen = computed(() => {
  if (!allExtracted.value) return true;
  if (anyBusy.value || loginPolling.value) return true;
  if (loadError.value) return true;
  if (Object.values(slotErrors).some(Boolean)) return true;
  for (const key of Object.keys(srcSlotState)) {
    const state = srcSlotState[key] || {};
    if (Object.values(state).some((s) => s.error)) return true;
  }
  // Host-driven extra sources (auto-extract flows) may flag busy/error
  // on the source object itself.
  return (props.extraSources || []).some((s) => s.busy || Boolean(s.error));
});

// Single common control shared by all boxes. Surfaces RoutingTools login
// when cookies are missing, otherwise a "Recuperar archivos" button that
// pulls every pending slot across the manifest and all extra sources.
const topAction = computed(() => {
  if (!filteredStations.value.length) return { show: false };

  // Nothing selected yet → keep visible but disabled with a clear hint.
  if (!cycleContextReady.value) {
    return {
      show: true,
      auth: false,
      msg: t("planManifest.actionSelectCycle"),
      label: t("planManifest.actionFetchFiles"),
      disabled: true,
    };
  }

  // Recovery in progress.
  if (anyBusy.value) {
    return {
      show: true,
      auth: false,
      busy: true,
      msg: t("planManifest.fetchingShort"),
      label: t("planManifest.fetchingShort"),
      disabled: true,
    };
  }

  // RoutingTools identification required for the manifest.
  if (props.showManifest && !cookiesInfo.value.ready) {
    if (loginPolling.value) {
      return {
        show: true,
        auth: true,
        msg: t("planManifest.actionWaitingSession"),
        label: t("planManifest.loginWaiting"),
        disabled: true,
      };
    }
    return {
      show: true,
      auth: true,
      msg: t("planManifest.actionLoginNeeded", {
        missing: (cookiesInfo.value.missing || []).join(", ") || "amzn_sso_token",
      }),
      label: t("planManifest.actionLoginAndFetch"),
      disabled: false,
    };
  }

  // Authed + cycle selected → recover everything pending.
  return {
    show: true,
    auth: false,
    msg: t("planManifest.actionFetchReadyFiles", { count: totalPending.value }),
    label: t("planManifest.actionFetchFiles"),
    disabled: totalPending.value === 0,
  };
});

function onTopAction() {
  if (props.showManifest && !cookiesInfo.value.ready) {
    postLoginAction = async () => {
      try {
        loginPollingTab?.close();
      } catch (_err) {
        /* no-op */
      }
      loginPollingTab = null;
      await reload();
      await recoverAll();
    };
    onLoginAndFetch();
    return;
  }
  recoverAll();
}

// Recover every pending slot across all boxes: manifest first (when
// shown), then each extra source in turn.
async function recoverAll() {
  if (props.showManifest) await recoverManifestPending();
  for (const box of boxes.value) {
    if (box.kind === "manifest") continue;
    await recoverExtraPending(box);
  }
}

// Recover every pending manifest slot in one go. Routes through the
// shared login flow first when RoutingTools cookies are missing.
async function recoverManifestPending() {
  if (!cookiesInfo.value.ready) {
    postLoginAction = async () => {
      try {
        loginPollingTab?.close();
      } catch (_err) {
        /* no-op */
      }
      loginPollingTab = null;
      await reload();
      await recoverManifestPending();
    };
    await onLoginAndFetch();
    return;
  }
  const targets = pendingSlots.value.slice();
  if (!targets.length) return;
  busyStation.value = "ALL";
  busyAction.value = "fetch";
  let auth = false;
  for (const slot of targets) {
    const res = await _runFetch(slot.stationCode, slot.cycle);
    if (res.auth) {
      auth = true;
      break;
    }
  }
  busyStation.value = "";
  busyAction.value = "";
  busyCycle.value = "";
  if (!auth) await reload();
}

// Recover every pending slot for an extra source (DA Assignment, ...).
async function recoverExtraPending(box) {
  const targets = slotsForBox(box).filter((s) => !s.saved);
  if (!targets.length) return;
  boxBusy[box.key] = true;
  try {
    for (const slot of targets) {
      const res = await _runExtraFetch(box, slot.stationCode, slot.cycle);
      if (res?.auth) break;
    }
  } finally {
    boxBusy[box.key] = false;
  }
}

// Fetch a single (DS, cycle) tuple for an extra source. The host owns
// the actual HTTP call and returns { ok | authError, count, name, ... }.
async function _runExtraFetch(box, stationCode, cycle) {
  if (typeof box.fetch !== "function") return { ok: false };
  const slotKey = `${stationCode}-${cycle}`;
  const st = _srcSlot(box.key, slotKey);
  st.error = "";
  st.busy = true;
  try {
    const res = await box.fetch({ stationCode, cycle, date: selectedDate.value });
    if (res?.authError) {
      st.error = res.message || t("planManifest.cookieReason");
      const url = res.loginUrl || box.loginUrl;
      if (url) {
        try {
          window.open(url, "_blank", "noopener");
        } catch (_err) {
          /* no-op */
        }
      }
      return { auth: true };
    }
    if (res?.ok) {
      st.saved = true;
      st.count = Number(res.count) || 0;
      st.name = res.name || "";
      return { ok: true };
    }
    st.error = res?.message || t("planManifest.fetchFailed");
    return { ok: false };
  } catch (err) {
    st.error = err?.message || t("planManifest.fetchFailed");
    return { ok: false };
  } finally {
    st.busy = false;
  }
}

function onSrcSlotFetch(box, slot) {
  return _runExtraFetch(box, slot.stationCode, slot.cycle);
}

// Manual upload for a single extra-source slot (optional per source).
async function onSrcSlotFile(box, slot, event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file || typeof box.upload !== "function") return;
  const slotKey = `${slot.stationCode}-${slot.cycle}`;
  const st = _srcSlot(box.key, slotKey);
  st.error = "";
  st.busy = true;
  try {
    const res = await box.upload({
      file,
      stationCode: slot.stationCode,
      cycle: slot.cycle,
      date: selectedDate.value,
    });
    if (res?.ok) {
      st.saved = true;
      st.count = Number(res.count) || 0;
      st.name = res.name || file.name;
    } else {
      st.error = res?.message || t("planManifest.uploadFailed");
    }
  } catch (err) {
    st.error = err?.message || t("planManifest.uploadFailed");
  } finally {
    st.busy = false;
  }
}

// ─── Login + auto-fetch ───────────────────────────────────
function _stopLoginPolling() {
  if (loginPollingTimer) {
    clearInterval(loginPollingTimer);
    loginPollingTimer = null;
  }
  loginPolling.value = false;
}

async function onLoginAndFetch() {
  const baseUrl = cookiesInfo.value.login_url || "https://routingtools.amazon.com/";
  // Append the active site so the operator lands on the correct station.
  const site = linkSite.value;
  const url = site && !baseUrl.includes("stationCode")
    ? `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}stationCode=${encodeURIComponent(site)}`
    : baseUrl;
  try {
    loginPollingTab = window.open(url, "routingtools-login");
  } catch (_err) {
    loginPollingTab = null;
  }
  if (loginPollingTab) {
    try {
      loginPollingTab.focus();
    } catch (_err) {
      /* no-op */
    }
  }

  if (!postLoginAction) {
    postLoginAction = async () => {
      try {
        loginPollingTab?.close();
      } catch (_err) {
        /* no-op */
      }
      loginPollingTab = null;
      await reload();
    };
  }

  if (loginPolling.value) return;
  loginPolling.value = true;

  let elapsed = 0;
  const intervalMs = 3000;
  const maxMs = 5 * 60 * 1000;

  loginPollingTimer = setInterval(async () => {
    elapsed += intervalMs;
    try {
      const status = await manifestCookiesStatus();
      cookiesInfo.value = status;
      if (status?.ready) {
        _stopLoginPolling();
        const action = postLoginAction;
        postLoginAction = null;
        if (action) await action();
        return;
      }
    } catch (_err) {
      /* keep polling */
    }
    if (elapsed >= maxMs) {
      _stopLoginPolling();
      postLoginAction = null;
    }
  }, intervalMs);
}

// ─── Fetch one or many ────────────────────────────────────
async function _runFetch(stationCode, cycle) {
  const key = `${stationCode}-${cycle}`;
  delete slotErrors[key];
  busyCycle.value = cycle;
  try {
    const result = await manifestFetch({
      stationCode,
      cycle,
      date: selectedDate.value,
    });
    if (result?.status === "auth_error") {
      cookiesInfo.value = {
        ...cookiesInfo.value,
        ready: false,
        login_url: result.login_url || cookiesInfo.value.login_url || "",
      };
      slotErrors[key] = result.message || t("planManifest.cookieReason");
      return { ok: false, auth: true, message: result.message };
    }
    emit("saved", {
      stationCode,
      cycle: result?.cycle || cycle,
      path: result?.path || "",
      name: result?.name || "",
      source: "routingtools",
    });
    return { ok: true };
  } catch (error) {
    slotErrors[key] = error?.message || t("planManifest.fetchFailed");
    return { ok: false, message: error?.message };
  }
}

async function fetchOne(slot) {
  busyStation.value = slot.stationCode;
  busyAction.value = "fetch";
  await _runFetch(slot.stationCode, slot.cycle);
  busyStation.value = "";
  busyAction.value = "";
  busyCycle.value = "";
  await reload();
}

async function onSlotFetch(slot) {
  if (!cookiesInfo.value.ready) {
    postLoginAction = async () => {
      try {
        loginPollingTab?.close();
      } catch (_err) {
        /* no-op */
      }
      loginPollingTab = null;
      await reload();
      await fetchOne(slot);
    };
    await onLoginAndFetch();
    return;
  }
  await fetchOne(slot);
}

// ─── Manual upload per slot ───────────────────────────────
async function onSlotFile(slot, event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  await uploadFile(slot, file);
}

async function onSlotDrop(slot, event) {
  dragoverSlotKey.value = "";
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  await uploadFile(slot, file);
}

async function uploadFile(slot, file) {
  const key = `${slot.stationCode}-${slot.cycle}`;
  delete slotErrors[key];
  busyStation.value = slot.stationCode;
  busyAction.value = "upload";
  busyCycle.value = slot.cycle;
  try {
    const result = await manifestUpload({
      file,
      stationCode: slot.stationCode,
      cycle: slot.cycle,
      date: selectedDate.value,
    });
    emit("saved", {
      stationCode: slot.stationCode,
      cycle: result?.cycle || slot.cycle,
      path: result?.path || "",
      name: result?.name || file.name,
      source: "upload",
    });
    await reload();
  } catch (error) {
    slotErrors[key] = error?.message || t("planManifest.uploadFailed");
  } finally {
    busyStation.value = "";
    busyAction.value = "";
    busyCycle.value = "";
  }
}

// ─── Selection-change emission ────────────────────────────
function selectedManifests() {
  const out = [];
  for (const station of stations.value) {
    for (const m of station.manifests) {
      const cycle = (m.cycle || "").toUpperCase();
      // A downloaded file that has been unmarked must never load, no
      // matter that the file still exists on the drive.
      if (isDeselected(station.code, cycle)) continue;
      out.push({
        stationCode: station.code,
        cycle,
        path: m.path,
        name: m.name,
        size: m.size,
        modified_at: m.modified_at,
      });
    }
  }
  return out;
}

function emitSelectionChange() {
  const payload = {
    date: selectedDate.value,
    manifests: selectedManifests(),
    // Every (station, cycle) currently MARKED in the panel: saved files
    // that haven't been unmarked, plus pending picks. Hosts that
    // accumulate extra sources (DA Assignment / Route Sheets) per cycle
    // use this to drop the buckets of any cycle the operator unmarked.
    cycles: visibleSlots.value.map((s) => ({
      stationCode: s.stationCode,
      cycle: s.cycle,
    })),
  };
  // Push to the global store so per-tool ManifestField dropzones can
  // turn green without their own HTTP roundtrip. setSnapshot only reads
  // `date` + `manifests`; the extra `cycles` key is ignored there.
  setManifestSnapshot(payload);
  emit("selection-change", payload);
}

// ─── Reload ───────────────────────────────────────────────
async function reload() {
  loading.value = true;
  cookiesChecking.value = true;
  setManifestLoading(true);
  loadError.value = "";
  try {
    const [statusPayload, cyclesPayload, cookiesPayload] = await Promise.all([
      manifestStatus({ date: selectedDate.value }),
      fetchCycles().catch(() => null),
      manifestCookiesStatus().catch(() => null),
    ]);

    if (cyclesPayload?.cycles?.length) {
      cycles.value = cyclesPayload.cycles;
      defaultCycle.value = cyclesPayload.default || cycles.value[0];
    }
    if (cookiesPayload) cookiesInfo.value = cookiesPayload;

    stations.value = (statusPayload?.stations || []).map((station) => ({
      code: String(station.code || "").toUpperCase(),
      manifests: Array.isArray(station.manifests) ? station.manifests : [],
      any_found: Boolean(station.any_found),
    }));
    extras.value = statusPayload?.extras || [];
    legacy.value = statusPayload?.legacy || [];

    for (const station of stations.value) _ensureSelection(station.code);

    emit("change", { stations: stations.value, date: selectedDate.value });
    emit("ready", stations.value.every((s) => s.any_found));
    emitSelectionChange();
  } catch (error) {
    loadError.value = error?.message || t("planManifest.loadFailed");
  } finally {
    loading.value = false;
    cookiesChecking.value = false;
    setManifestLoading(false);
  }
}

function formatSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function formatModified(epoch) {
  const ts = Number(epoch) || 0;
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (_err) {
    return "";
  }
}

// Reset all per-station selection/deselection bookkeeping. Used when the
// date or site filter changes so unmarks from a previous context don't
// leak onto a different day's files.
function _resetSelectionState() {
  for (const k of Object.keys(selections)) delete selections[k];
  for (const k of Object.keys(deselected)) delete deselected[k];
}

// Reload when the resolved date changes (global navbar date, prop override, or offset).
watch(selectedDate, (newVal, oldVal) => { if (newVal !== oldVal && _alive) { _resetSelectionState(); reload(); } });

// Reload when the site filter changes.
watch(panelSite, (newVal, oldVal) => { if (newVal !== oldVal && _alive) { _resetSelectionState(); reload(); } });

let _alive = false;
onMounted(() => {
  _alive = true;
  if (props.autoload) reload();
});

onBeforeUnmount(() => {
  _alive = false;
  _stopLoginPolling();
  try {
    loginPollingTab?.close();
  } catch (_err) {
    /* no-op */
  }
  loginPollingTab = null;
});

defineExpose({ reload });
</script>

<style scoped>
.pmp-wrap {
  /* width explícito: dentro de un flex-column (p. ej. .tool-surface de
     HUB Dropped) los márgenes auto del eje transversal anulan el
     stretch y el fold plegado se encogía a una píldora estrecha. Con
     100% ocupa la misma columna de 1320px que el resto de containers. */
  width: 100%;
  max-width: 1320px;
  margin: 0 auto 16px;
}

/* Lives inside the CollapsedSection body now (the fold provides the
   container/border), so no glass panel nor big padding of its own. */
.plan-manifest-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 6px 2px 2px;
}

.plan-manifest-panel.is-loading {
  opacity: 0.7;
}

.pmp-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.pmp-head__main h3 {
  margin: 4px 0 2px;
  font-size: 16px;
}

.pmp-head__main .hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--toolbox-text-muted);
  max-width: 720px;
}

.pmp-head__controls {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.badge--manifest {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  background: rgba(245, 158, 11, 0.18);
  color: #b45309;
  margin-bottom: 4px;
}

/* Compact icon button on the folded bar — same footprint as
   ExtractMiniButton (.tb-extract-mini) used by the other connection
   sections. */
.pmp-reconnect-mini {
  padding: 5px 9px;
  font-size: 13px;
  line-height: 1;
}

.pmp-connection-state {
  padding-left: 8px;
  border-left: 1px solid currentColor;
}

.pmp-connection-state--connected {
  color: #16a34a;
}

.pmp-connection-state--checking {
  color: #d97706;
}

.pmp-connection-state--disconnected {
  color: var(--toolbox-danger);
}

.pmp-actions-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 10px 14px;
  border-radius: 10px;
  background: rgba(34, 197, 94, 0.08);
  border: 1px solid rgba(34, 197, 94, 0.25);
}

.pmp-actions-strip--auth {
  background: rgba(245, 158, 11, 0.1);
  border-color: rgba(245, 158, 11, 0.35);
}

.pmp-actions-strip__msg {
  margin: 0;
  font-size: 12.5px;
  color: var(--toolbox-text);
}

.pmp-actions-strip__busy {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--toolbox-success);
  white-space: nowrap;
}

.pmp-progress {
  position: relative;
  flex: 0 0 220px;
  height: 26px;
  border-radius: 999px;
  overflow: hidden;
  background: rgba(34, 197, 94, 0.18);
  border: 1px solid rgba(34, 197, 94, 0.35);
}

.pmp-progress__fill {
  position: absolute;
  inset: 0 auto 0 0;
  width: 0;
  background: linear-gradient(90deg, #16a34a, #22c55e);
  transition: width 0.25s ease;
}

.pmp-progress__label {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: var(--toolbox-success);
  white-space: nowrap;
}

.pmp-stations {
  display: grid;
  gap: 14px;
  grid-template-columns: 1fr;
}

@media (min-width: 900px) {
  .pmp-stations {
    grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  }
}

.pmp-station {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  border-radius: 12px;
  border: 1px solid rgb(var(--toolbox-border-rgb, 0 0 0) / 18%);
  background: rgb(var(--toolbox-surface-rgb, 255 255 255) / 60%);
}

.pmp-station__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.pmp-station__head h4 {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
}

.pmp-pill {
  padding: 2px 10px;
  border-radius: 999px;
  font-weight: 700;
  font-size: 11px;
}

.pmp-pill--ok {
  background: rgba(34, 197, 94, 0.18);
  color: var(--toolbox-success);
}

.pmp-pill--miss {
  background: rgba(0, 0, 0, 0.08);
  color: var(--toolbox-text-muted);
}

.pmp-cycle-grid {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}

.pmp-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  min-height: 24px;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgb(var(--toolbox-shadow-rgb) / 18%);
  background: rgb(var(--toolbox-surface-rgb) / 60%);
  cursor: pointer;
  font-family: inherit;
  font-size: 11px;
  line-height: 1.4;
  font-weight: 600;
  color: var(--toolbox-text);
  white-space: nowrap;
  transition: background 0.12s, border-color 0.12s, color 0.12s;
}

.pmp-chip:hover {
  border-color: rgba(245, 158, 11, 0.6);
  background: rgba(245, 158, 11, 0.08);
}

.pmp-chip:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.pmp-chip--saved {
  border-color: rgba(34, 197, 94, 0.55);
  background: rgba(34, 197, 94, 0.16);
  color: var(--toolbox-success);
}

.pmp-chip--saved:hover {
  border-color: rgba(34, 197, 94, 0.9);
  background: rgba(34, 197, 94, 0.24);
}

.pmp-chip--selected:not(.pmp-chip--saved) {
  border-color: rgba(245, 158, 11, 0.85);
  background: rgba(245, 158, 11, 0.18);
  color: #b45309;
}

.pmp-chip--selected.pmp-chip--saved {
  box-shadow: 0 0 0 2px rgba(34, 197, 94, 0.4);
}

/* Saved file the operator has unmarked: greyed out, won't load. */
.pmp-chip--off {
  border-color: rgba(0, 0, 0, 0.18);
  background: rgba(0, 0, 0, 0.05);
  color: var(--toolbox-text-muted);
  text-decoration: line-through;
  opacity: 0.7;
}

.pmp-chip--off:hover {
  border-color: rgba(34, 197, 94, 0.6);
  background: rgba(34, 197, 94, 0.08);
  text-decoration: none;
  opacity: 1;
}

.pmp-chip.is-busy {
  background: repeating-linear-gradient(
    -45deg,
    rgba(245, 158, 11, 0.18) 0 6px,
    rgba(245, 158, 11, 0.06) 6px 12px
  );
}

.pmp-chip__check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 10px;
  line-height: 1;
  font-weight: 800;
  font-size: 10px;
}

.pmp-slots {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-top: 1px dashed rgb(var(--toolbox-border-rgb, 0 0 0) / 18%);
  margin-top: 4px;
  padding-top: 14px;
}

.pmp-slots__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.pmp-slots__head h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 700;
  color: var(--toolbox-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.pmp-slots__counter {
  font-size: 11px;
  color: var(--toolbox-text-muted);
  font-style: italic;
}

.pmp-slot-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
}

.pmp-slot {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid rgb(var(--toolbox-shadow-rgb) / 12%);
  background: rgb(var(--toolbox-surface-rgb) / 45%);
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
  min-height: 148px;
}

.pmp-slot--ok {
  border-color: rgba(34, 197, 94, 0.45);
  background: rgba(34, 197, 94, 0.08);
}

.pmp-slot--pending {
  border: 1px dashed rgba(245, 158, 11, 0.55);
  background: rgba(245, 158, 11, 0.05);
}

.pmp-slot--invalid {
  border: 2px solid rgba(220, 38, 38, 0.7);
  background: rgba(220, 38, 38, 0.08);
}

.pmp-slot.is-dragover {
  border-color: rgba(245, 158, 11, 0.9);
  background: rgba(245, 158, 11, 0.12);
}

.pmp-slot.is-busy {
  background: repeating-linear-gradient(
    -45deg,
    rgba(245, 158, 11, 0.16) 0 8px,
    rgba(245, 158, 11, 0.04) 8px 16px
  );
}

.pmp-slot__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.pmp-slot__station {
  font-weight: 700;
  font-size: 13px;
  color: var(--toolbox-text);
}

.pmp-slot__cycle {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 700;
  background: rgba(0, 0, 0, 0.05);
  padding: 1px 8px;
  border-radius: 4px;
  color: var(--toolbox-text-muted);
}

.pmp-slot__status {
  font-size: 10.5px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.pmp-slot__status--ok {
  background: rgba(34, 197, 94, 0.22);
  color: var(--toolbox-success);
}

.pmp-slot__status--pending {
  background: rgba(245, 158, 11, 0.22);
  color: #b45309;
}

.pmp-slot__status--invalid {
  background: rgba(220, 38, 38, 0.18);
  color: var(--toolbox-danger);
}

.pmp-slot__body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pmp-slot__name {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: block;
}

.pmp-slot__meta {
  margin: 0;
  font-size: 10.5px;
  color: var(--toolbox-text-muted);
}

.pmp-slot__meta--invalid {
  color: var(--toolbox-danger);
  font-weight: 700;
}

.pmp-slot__invalid-hint {
  margin: 2px 0 0;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--toolbox-danger);
  line-height: 1.3;
}

.pmp-slot__hint {
  margin: 0;
  font-size: 11px;
  color: var(--toolbox-text-muted);
  font-style: italic;
}

.pmp-slot__actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: auto;
  padding-top: 4px;
}

.pmp-slot__upload {
  position: relative;
  display: inline-block;
  cursor: pointer;
}

.pmp-slot__upload input[type="file"] {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.tb-button--sm {
  padding: 4px 10px !important;
  font-size: 11.5px !important;
}

.pmp-slot__error {
  margin: 0;
  font-size: 11px;
}

.pmp-slot--src.pmp-slot--pending {
  border-style: solid;
  border-color: rgba(59, 130, 246, 0.4);
  background: rgba(59, 130, 246, 0.05);
}

/* Recovered extra source — green + checkmark, like a saved manifest. */
.pmp-slot--src.pmp-slot--ok {
  border-color: rgba(34, 197, 94, 0.55);
  background: rgba(34, 197, 94, 0.1);
}

.pmp-src-body {
  flex-direction: row;
  align-items: center;
  gap: 10px;
}

.pmp-src-check {
  width: 30px;
  height: 30px;
  border-radius: 999px;
  background: #2ea84a;
  color: #fff;
  display: inline-grid;
  place-items: center;
  font-weight: 700;
  font-size: 15px;
  flex-shrink: 0;
}

.pmp-src-info {
  min-width: 0;
}

/* ─── Source boxes (Plan Manifest · DA Assignment · Route Sheets) ─── */
.pmp-boxes {
  display: grid;
  gap: 14px;
  grid-template-columns: 1fr;
}

@media (min-width: 900px) {
  .pmp-boxes {
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  }
}

.pmp-box {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  border-radius: 12px;
  border: 1px solid rgb(var(--toolbox-border-rgb, 0 0 0) / 18%);
  background: rgb(var(--toolbox-surface-rgb, 255 255 255) / 55%);
}

.pmp-box--src {
  border-color: rgba(59, 130, 246, 0.28);
  background: rgba(59, 130, 246, 0.04);
}

.pmp-box__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.pmp-box__title {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pmp-box__title h4 {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
}

.pmp-box__counter {
  font-size: 11px;
  color: var(--toolbox-text-muted);
  font-style: italic;
}

.pmp-box__auth {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin: 0;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  color: #b45309;
  background: rgba(245, 158, 11, 0.1);
  border: 1px solid rgba(245, 158, 11, 0.35);
}

.pmp-box__empty {
  margin: 0;
  padding: 14px;
  text-align: center;
  font-size: 12.5px;
  color: var(--toolbox-text-muted);
  border: 1px dashed rgba(245, 158, 11, 0.45);
  border-radius: 10px;
  background: rgba(245, 158, 11, 0.05);
}

.pmp-box .pmp-slot-grid {
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
}

.pmp-extras {
  margin-top: 6px;
  border-top: 1px dashed rgb(var(--toolbox-border-rgb, 0 0 0) / 18%);
  padding-top: 10px;
}

.pmp-extras summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--toolbox-text-muted);
  user-select: none;
  margin-bottom: 8px;
}

.pmp-extra-badge {
  font-size: 10px;
  background: rgba(0, 0, 0, 0.06);
  color: var(--toolbox-text-muted);
  padding: 2px 6px;
  border-radius: 4px;
  margin-left: 6px;
}

.pmp-station--extra {
  border-color: rgb(var(--toolbox-shadow-rgb) / 8%);
  background: rgb(var(--toolbox-surface-rgb) / 40%);
}

.pmp-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 4px;
}

.pmp-list--legacy {
  margin-top: 8px;
}

.pmp-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.03);
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.pmp-row--legacy {
  background: rgba(245, 158, 11, 0.08);
  border-color: rgba(245, 158, 11, 0.18);
}

.pmp-row__main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
}

.pmp-row__cycle {
  font-size: 11px;
  font-weight: 700;
  background: rgba(0, 0, 0, 0.04);
  padding: 2px 8px;
  border-radius: 4px;
  white-space: nowrap;
}

.pmp-row__cycle--legacy {
  background: rgba(245, 158, 11, 0.18);
  color: #b45309;
}

.pmp-row__name {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pmp-row__meta {
  font-size: 11px;
  color: var(--toolbox-text-muted);
  white-space: nowrap;
}

.error-text {
  color: var(--toolbox-danger);
  font-size: 12.5px;
  margin: 0;
}

.hint {
  font-size: 13px;
  color: var(--toolbox-text-muted);
}
</style>
