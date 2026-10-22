<template>
  <!-- STEM connection / refresh bar. Same folding pattern (and the same
       constrained container width) as the other extraction panels: forced
       open only while a download runs or there is no live STEM yet. Once
       connected it folds down to the status bar; errors and the login
       button surface ON the collapsed bar, so the panel never needs to
       stay expanded. -->
  <div class="stem-panel">
    <CollapsedSection
      :label="t('stemPanel.title')"
      :force-open="forceOpen"
      :sticky-actions="hasExtraActions"
    >
      <template #summary>
        <span class="stem-dot" :class="dotClass"></span>
        <span :class="{ 'stem-summary--error': showError }">{{ summaryLine }}</span>
      </template>
      <!-- Collapsed-bar actions: same compact icon buttons as the other
           connection sections (ExtractMiniButton pattern): the full
           label lives in the tooltip/aria-label. Tools can inject extra
           mini buttons via the ``actions`` slot — those stay visible in
           both fold states (sticky), while the built-in refresh/login
           minis only show collapsed (expanded, the body already has the
           full-size buttons). -->
      <template #actions="{ open }">
        <slot name="actions" />
        <template v-if="!open">
          <ExtractMiniButton
            :busy="refreshing"
            :label="t('stemPanel.refresh')"
            @extract="emit('refresh')"
          />
          <button
            v-if="loginUrl"
            class="tb-button tb-button--primary stem-mini"
            type="button"
            :title="t('stemPanel.openLogin')"
            :aria-label="t('stemPanel.openLogin')"
            @click="emit('open-login')"
          >
            <span aria-hidden="true">🔑</span>
          </button>
        </template>
      </template>

      <div class="stem-row">
        <div class="stem-row__info">
          <span class="stem-dot" :class="dotClass"></span>
          <span class="stem-row__text">{{ statusLine }}</span>
        </div>
        <div class="stem-row__actions">
          <button
            class="tb-button tb-button--ghost"
            type="button"
            :disabled="refreshing"
            @click="emit('refresh')"
          >
            {{ refreshing ? t("stemPanel.refreshing") : `🔄 ${t("stemPanel.refresh")}` }}
          </button>
          <button
            v-if="loginUrl"
            class="tb-button tb-button--ghost"
            type="button"
            @click="emit('open-login')"
          >
            🔑 {{ t("stemPanel.openLogin") }}
          </button>
        </div>
      </div>
      <p
        v-if="statusText"
        class="stem-note"
        :class="{ 'stem-note--error': statusOk === false }"
      >
        {{ statusText }}
      </p>
    </CollapsedSection>
  </div>
</template>

<script setup>
import { computed, useSlots } from "vue";
import CollapsedSection from "./CollapsedSection.vue";
import ExtractMiniButton from "./ExtractMiniButton.vue";
import { useI18n } from "../i18n";

const props = defineProps({
  /** "stem" (live download), "config" (configured fallback) or "". */
  source: { type: String, default: "" },
  /** ISO timestamp of the last live download. */
  fetchedAt: { type: String, default: "" },
  /** A download is in flight (disables the button, keeps panel open). */
  refreshing: { type: Boolean, default: false },
  /** Federate login URL when the STEM session expired. */
  loginUrl: { type: String, default: "" },
  /** Optional result/error message shown under the row. */
  statusText: { type: String, default: "" },
  /** true/false colours the statusText note; null = neutral (no type
      check: tools keep it in a ref(null)). */
  statusOk: { default: null },
});

const emit = defineEmits(["refresh", "open-login"]);

const slots = useSlots();
// Only go sticky when the consumer actually injects extra actions:
// otherwise the bar behaves exactly as before (collapsed-only minis).
const hasExtraActions = computed(() => Boolean(slots.actions));

const { t } = useI18n();

const prettyFetchedAt = computed(() =>
  String(props.fetchedAt || "").replace("T", " ").slice(0, 16)
);

const statusLine = computed(() => {
  if (props.source === "stem") {
    return t("stemPanel.live", { time: prettyFetchedAt.value });
  }
  if (props.source === "config") return t("stemPanel.config");
  return t("stemPanel.none");
});

const showError = computed(
  () => Boolean(props.statusText) && props.statusOk === false
);

// Collapsed-bar text: a failure takes the slot (stays visible while
// folded); otherwise the normal connection line.
const summaryLine = computed(() =>
  showError.value ? props.statusText : statusLine.value
);

const dotClass = computed(() => {
  if (showError.value) return "stem-dot--off";
  if (props.source === "stem") return "stem-dot--ok";
  if (props.source === "config") return "stem-dot--warn";
  return "stem-dot--off";
});

// Open only while working or while there is no live STEM yet. Once
// connected ("extracted") the panel folds like the other extraction
// panels — errors and the login button live on the collapsed bar.
const forceOpen = computed(() => props.refreshing || props.source !== "stem");
</script>

<style scoped>
/* Same constrained column as the tool panels around it. */
.stem-panel {
  max-width: 1320px;
  margin: 0 auto 16px;
}

.stem-summary--error {
  color: #d9534f;
  font-weight: 600;
}

.stem-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}

.stem-row__info {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.stem-row__text {
  font-size: 13px;
  color: var(--toolbox-muted);
}

.stem-row__actions {
  display: flex;
  gap: 8px;
}

.stem-dot {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--toolbox-muted);
}

.stem-dot--ok { background: #22c55e; }
.stem-dot--warn { background: #f59e0b; }
.stem-dot--off { background: #9ca3af; }

/* Compact icon button on the folded bar — same footprint as
   ExtractMiniButton (.tb-extract-mini). */
.stem-mini {
  padding: 5px 9px;
  font-size: 13px;
  line-height: 1;
}

.stem-note {
  margin: 0;
  font-size: 12.5px;
  color: var(--toolbox-muted);
}

.stem-note--error {
  color: #d9534f;
}
</style>
