<template>
  <div class="tb-conn-panel" :class="{ 'is-min': minimized }">
    <div class="tb-conn-head">
      <span class="tb-conn-title">{{ title || t("common.connectionsTitle") }}</span>
      <div class="tb-conn-actions">
        <!-- Self-contained status chips, one per source (Flow Meeting NS style):
             dot + name + state, and an inline "Conectar" action when the source
             is down. Rendered inline next to the action button. -->
        <div v-if="showStatus" class="tb-conn-chips">
          <span
            v-for="s in identity.sources"
            :key="s.id"
            class="tb-conn-chip"
            :class="`is-${identity.statusOf(s.id)}`"
            role="status"
            :title="`${chipLabel(s)}: ${chipStateLabel(s.id)}`"
          >
            <span class="tb-conn-chip__dot" aria-hidden="true"></span>
            <span class="tb-conn-chip__name">{{ chipLabel(s) }}</span>
            <span class="tb-conn-chip__state">{{ chipStateLabel(s.id) }}</span>
            <!-- Kept during "reconnecting" too: the auto-reconnect loop may be
                 polling with its login tab popup-blocked or lost — the click
                 re-opens it under a fresh user gesture. -->
            <button
              v-if="identity.statusOf(s.id) !== 'connected'"
              type="button"
              class="tb-conn-chip__action"
              :title="t('common.connect')"
              @click="identity.reconnect(s.id)"
            >
              {{ t("common.connect") }}
            </button>
          </span>
        </div>

        <!-- "Reconectar todo": reconnects every source that is currently down
             (chips can still connect individually). Hidden once everything is
             connected — at that point only "Extraer" remains, occupying the
             same slot the connect button used. -->
        <button
          v-if="!allConnected"
          type="button"
          class="tb-button tb-button--primary"
          :disabled="anyReconnecting"
          :title="reconnectAllLabel"
          @click="identity.identify()"
        >
          <span aria-hidden="true">🔐</span>
          {{ reconnectAllLabel }}
        </button>

        <!-- Single "Extraer" (optional). Only shown once every source is
             identified/connected — hidden while the connect button is active. -->
        <button
          v-if="showExtract && allConnected"
          type="button"
          class="tb-button tb-button--primary"
          :disabled="extractBusy || extractDisabled"
          :title="extractLabel"
          @click="$emit('extract')"
        >
          <span aria-hidden="true">📡</span>
          {{ extractBusy ? t("common.extracting") : extractLabel }}
        </button>

        <slot name="actions"></slot>

        <!-- Expand/fold toggle: only relevant while everything is
             connected (the panel is minimized then) and there is body
             content (source cards, etc.) to reveal. -->
        <button
          v-if="allConnected && hasBody"
          type="button"
          class="tb-conn-toggle"
          :aria-expanded="expanded ? 'true' : 'false'"
          @click="expanded = !expanded"
        >
          {{ expanded ? "▾" : "▸" }}
        </button>
      </div>
    </div>

    <!-- Body (per-source cards and any extra content). Hidden while the
         panel is minimized: the chips in the bar already carry the
         status, which is all that matters once connected. -->
    <div v-if="hasBody" v-show="!minimized" class="tb-conn-body">
      <slot></slot>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, useSlots } from "vue";

import { useI18n } from "../i18n";

/**
 * One container per tool grouping a single "Conectar" button (connects only the
 * disconnected sources) and, optionally, a single "Extraer" button, plus small
 * read-only status chips (one per source, Flow-Meeting style).
 */
const props = defineProps({
  identity: { type: Object, required: true },
  title: { type: String, default: "" },
  /** Render the small per-source status chips. */
  showStatus: { type: Boolean, default: false },
  /** Show the single "Extraer" button in the header. */
  showExtract: { type: Boolean, default: false },
  /** "none" | "partial" | "all" → drives the extract button label. */
  extractState: {
    type: String,
    default: "none",
    validator: (v) => ["none", "partial", "all"].includes(v),
  },
  extractDisabled: { type: Boolean, default: false },
  extractBusy: { type: Boolean, default: false },
});

defineEmits(["extract"]);

const { t } = useI18n();

const allConnected = computed(() => Boolean(props.identity.allConnected.value));
const anyReconnecting = computed(() => Boolean(props.identity.anyReconnecting.value));
const total = computed(() => props.identity.sources.length);

const slots = useSlots();
const hasBody = computed(() => Boolean(slots.default));

// Minimized once every source is connected: the panel folds into a slim
// bar with the title, the status chips and the action buttons on the
// right. While something is disconnected/reconnecting the full panel
// (with the body content) stays visible so the operator can act.
const expanded = ref(false);
const minimized = computed(() => allConnected.value && !expanded.value);

const reconnectAllLabel = computed(() => {
  if (anyReconnecting.value) return t("common.identifying");
  if (allConnected.value) return t("common.allConnected");
  return t("common.reconnectAll");
});

const extractLabel = computed(() => {
  if (props.extractState === "all") return t("common.reextractAll");
  if (props.extractState === "partial") return t("common.extractRest");
  return total.value > 1 ? t("common.extractAll") : t("common.extract");
});

// Chip name resolved at render time: sources that pass `labelKey` get the
// active locale on every render (a plain `label` captured t() output at
// setup time and went stale after a locale switch).
function chipLabel(source) {
  return source.labelKey ? t(source.labelKey) : source.label;
}

function chipStateLabel(id) {
  const s = props.identity.statusOf(id);
  if (s === "connected") return t("common.connected");
  if (s === "reconnecting") return t("common.reconnecting");
  return t("common.disconnected");
}
</script>

<style scoped>
.tb-conn-panel {
  border: 1px solid rgb(var(--toolbox-border-rgb) / 60%);
  border-radius: 14px;
  padding: 14px;
  margin-bottom: 16px;
  background: rgb(var(--toolbox-surface-rgb) / 40%);
}

/* Minimized (everything connected): slim bar — title left, status chips
   and action buttons right. */
.tb-conn-panel.is-min {
  padding: 7px 12px;
}

.tb-conn-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.tb-conn-panel.is-min .tb-conn-head,
.tb-conn-panel:not(:has(.tb-conn-body)) .tb-conn-head {
  margin-bottom: 0;
}

.tb-conn-toggle {
  display: inline-grid;
  place-items: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 55%);
  border-radius: 6px;
  background: transparent;
  color: var(--toolbox-muted);
  font-size: 12px;
  cursor: pointer;
}

.tb-conn-toggle:hover {
  color: var(--toolbox-text);
  border-color: var(--toolbox-accent);
}

.tb-conn-title {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--toolbox-muted);
}

.tb-conn-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* ── Small status chips (Flow Meeting style) ── */
.tb-conn-chips {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.tb-conn-chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 55%);
  background: rgb(var(--toolbox-surface-rgb) / 60%);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.tb-conn-chip__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--toolbox-muted);
  flex-shrink: 0;
}

.tb-conn-chip__name {
  letter-spacing: 0.01em;
}

.tb-conn-chip__state {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  opacity: 0.85;
}

.tb-conn-chip__action {
  margin-left: 2px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid currentColor;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  line-height: 1.4;
}
.tb-conn-chip__action:hover {
  background: rgb(208 63 63 / 12%);
}

.tb-conn-chip.is-connected {
  color: #1f6f49;
  border-color: rgb(46 166 90 / 45%);
}
.tb-conn-chip.is-connected .tb-conn-chip__dot {
  background: #2ea65a;
  box-shadow: 0 0 0 3px rgb(46 166 90 / 18%);
}

.tb-conn-chip.is-disconnected {
  color: var(--toolbox-danger);
  border-color: rgb(208 63 63 / 45%);
}
.tb-conn-chip.is-disconnected .tb-conn-chip__dot {
  background: #d03f3f;
  box-shadow: 0 0 0 3px rgb(208 63 63 / 18%);
}

.tb-conn-chip.is-reconnecting {
  color: #b8860b;
  border-color: rgb(224 167 46 / 50%);
}
.tb-conn-chip.is-reconnecting .tb-conn-chip__dot {
  background: #e0a72e;
  box-shadow: 0 0 0 3px rgb(224 167 46 / 20%);
  animation: tb-conn-pulse 1.3s ease-in-out infinite;
}

@keyframes tb-conn-pulse {
  0%, 100% { box-shadow: 0 0 0 3px rgb(224 167 46 / 22%); }
  50% { box-shadow: 0 0 0 6px rgb(224 167 46 / 6%); }
}

/* Backward-compat: any card still passed in the slot keeps sane spacing. */
.tb-conn-panel :deep(.ds-card:last-child) {
  margin-bottom: 0;
}
</style>
