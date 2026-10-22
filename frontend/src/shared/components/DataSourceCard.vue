<template>
  <section class="ds-card" :class="cardClasses">
    <div class="ds-card__main">
      <p class="ds-card__label">
        <span class="ds-card__dot" :class="dotClass" aria-hidden="true"></span>
        <span v-if="checked" class="ds-card__check" aria-hidden="true">✓</span>
        <span class="ds-card__name">{{ title }}</span>
        <span class="ds-card__chip" :class="chipClass">
          {{ chipText }}
        </span>
        <span v-if="rowCount && (ready || checked)" class="ds-card__count">{{ rowCount }}</span>
      </p>
      <p class="ds-card__status">{{ subtitle }}</p>
      <p v-if="error" class="ds-card__error">{{ error }}</p>
    </div>
    <div class="ds-card__actions">
      <slot name="actions"></slot>
    </div>
  </section>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  /** Display title (e.g. "Mercury", "PerfectMile", "Plan Manifest"). */
  title: {
    type: String,
    required: true,
  },
  /** Whether the data source is connected / loaded. */
  ready: {
    type: Boolean,
    default: false,
  },
  /**
   * Whether the source is mid-reconnection (login opened, re-probing until the
   * session comes back). Renders a pulsing amber dot + chip. Only takes effect
   * when the source is not already ``ready``/``checked``.
   */
  reconnecting: {
    type: Boolean,
    default: false,
  },
  /**
   * Visual accent. Maps to the per-source colour:
   *   - "mercury"     → purple (DSC)
   *   - "perfectmile" → orange (DEX)
   *   - "manifest"    → green (FTDS)
   *   - "harmony"     → teal (DINO)
   *   - "honeypot"    → amber (POD)
   *   - "neutral"     → default grey
   */
  accent: {
    type: String,
    default: "neutral",
    validator: (value) =>
      ["mercury", "perfectmile", "manifest", "harmony", "honeypot", "scc", "neutral"].includes(value),
  },
  /** Optional informational subtitle (e.g. "Sesion detectada en Firefox"). */
  subtitle: {
    type: String,
    default: "",
  },
  /** Optional error / warning message rendered in red below the subtitle. */
  error: {
    type: String,
    default: "",
  },
  /** Optional row count (when the source has already retrieved rows). */
  rowCount: {
    type: [String, Number],
    default: 0,
  },
  /** Customisable chip labels (when the default Connected / Disconnected
   *  is not the right wording — e.g. "Cargado" / "Pendiente" for files). */
  readyLabel: {
    type: String,
    default: "",
  },
  notReadyLabel: {
    type: String,
    default: "",
  },
  /** Chip label shown while ``reconnecting`` (e.g. "Reconectando…"). */
  reconnectingLabel: {
    type: String,
    default: "",
  },
  /**
   * Marks the source as fully retrieved/active. Forces a green "success"
   * styling and a ✓ checkmark regardless of accent (e.g. PerfectMile rows
   * extracted, Cortex auto-fetch active).
   */
  checked: {
    type: Boolean,
    default: false,
  },
  /** Optional chip label shown when ``checked`` (falls back to readyLabel). */
  checkedLabel: {
    type: String,
    default: "",
  },
});

// A source is "reconnecting" only when it is not already connected/loaded.
const isReconnecting = computed(
  () => props.reconnecting && !props.ready && !props.checked,
);

const cardClasses = computed(() => [
  `ds-card--${props.accent}`,
  {
    "is-ready": props.ready,
    "is-checked": props.checked,
    "is-reconnecting": isReconnecting.value,
  },
]);

const dotClass = computed(() => ({
  ready: props.ready,
  reconnecting: isReconnecting.value,
}));

const chipClass = computed(() => {
  if (props.checked) return "is-done";
  if (props.ready) return "is-ok";
  if (isReconnecting.value) return "is-reconnecting";
  return "is-warn";
});

// Localized chip defaults; callers can still override via props.
const chipText = computed(() => {
  const ready = props.readyLabel || t("components.dataSource.connected");
  if (props.checked) return props.checkedLabel || ready;
  if (props.ready) return ready;
  if (isReconnecting.value) {
    return props.reconnectingLabel || t("components.dataSource.reconnecting");
  }
  return props.notReadyLabel || t("components.dataSource.disconnected");
});
</script>

<style scoped>
.ds-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  margin-bottom: 12px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 56%);
  border-radius: 12px;
  background: rgb(var(--toolbox-surface-rgb) / 62%);
  transition: border-color 0.2s, background 0.2s, box-shadow 0.2s;
  flex-wrap: wrap;
}

.ds-card.is-ready.ds-card--manifest {
  border-color: rgb(60 118 61 / 45%);
  background: rgb(60 118 61 / 6%);
  box-shadow: 0 6px 14px rgb(60 118 61 / 10%);
}

/* Fully retrieved / active: green success state + checkmark. Wins over
   any per-accent ready styling so PerfectMile (orange) and Cortex turn
   green once their data is loaded. */
.ds-card.is-checked {
  border-color: rgb(29 154 99 / 55%) !important;
  background: rgb(29 154 99 / 9%) !important;
  box-shadow: 0 6px 14px rgb(29 154 99 / 16%) !important;
}

.ds-card.is-checked .ds-card__dot {
  background: #1d9a63 !important;
  box-shadow: 0 0 0 3px rgb(29 154 99 / 22%) !important;
}

.ds-card__check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 999px;
  background: #1d9a63;
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  line-height: 1;
}

.ds-card__chip.is-done {
  background: rgb(29 154 99 / 18%);
  color: #1d6f49;
}

.ds-card.is-ready.ds-card--mercury {
  border-color: rgb(110 67 145 / 50%);
  background: rgb(110 67 145 / 6%);
  box-shadow: 0 6px 14px rgb(110 67 145 / 12%);
}

.ds-card.is-ready.ds-card--perfectmile {
  border-color: rgb(196 111 29 / 55%);
  background: rgb(196 111 29 / 6%);
  box-shadow: 0 6px 14px rgb(196 111 29 / 12%);
}

.ds-card.is-ready.ds-card--harmony {
  border-color: rgb(31 111 139 / 50%);
  background: rgb(31 111 139 / 6%);
  box-shadow: 0 6px 14px rgb(31 111 139 / 12%);
}

.ds-card.is-ready.ds-card--honeypot {
  border-color: rgb(180 130 28 / 55%);
  background: rgb(180 130 28 / 6%);
  box-shadow: 0 6px 14px rgb(180 130 28 / 12%);
}

/* SCC (boson station dashboards) → blue. */
.ds-card.is-ready.ds-card--scc {
  border-color: rgb(37 99 208 / 52%);
  background: rgb(37 99 208 / 6%);
  box-shadow: 0 6px 14px rgb(37 99 208 / 12%);
}

.ds-card__main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1 1 240px;
}

.ds-card__label {
  margin: 0;
  font-weight: 700;
  font-size: 14px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ds-card__name {
  letter-spacing: 0.01em;
}

.ds-card__dot {
  width: 9px;
  height: 9px;
  border-radius: 999px;
  background: #c43d3d;
  box-shadow: 0 0 0 3px rgb(196 61 61 / 22%);
  transition: background 0.2s, box-shadow 0.2s;
}

.ds-card--manifest .ds-card__dot.ready {
  background: #1d9a63;
  box-shadow: 0 0 0 3px rgb(29 154 99 / 22%);
}

.ds-card--mercury .ds-card__dot.ready {
  background: #6e4391;
  box-shadow: 0 0 0 3px rgb(110 67 145 / 22%);
}

.ds-card--perfectmile .ds-card__dot.ready {
  background: #c46f1d;
  box-shadow: 0 0 0 3px rgb(196 111 29 / 22%);
}

.ds-card--harmony .ds-card__dot.ready {
  background: #1f6f8b;
  box-shadow: 0 0 0 3px rgb(31 111 139 / 22%);
}

.ds-card--honeypot .ds-card__dot.ready {
  background: #b8821c;
  box-shadow: 0 0 0 3px rgb(184 130 28 / 22%);
}

.ds-card--scc .ds-card__dot.ready {
  background: #2563d0;
  box-shadow: 0 0 0 3px rgb(37 99 208 / 22%);
}

.ds-card--neutral .ds-card__dot.ready {
  background: #1d9a63;
  box-shadow: 0 0 0 3px rgb(29 154 99 / 22%);
}

.ds-card__chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.ds-card__chip.is-ok {
  background: rgb(29 154 99 / 14%);
  color: #1d6f49;
}

.ds-card--mercury .ds-card__chip.is-ok {
  background: rgb(110 67 145 / 14%);
  color: #5b3079;
}

.ds-card--perfectmile .ds-card__chip.is-ok {
  background: rgb(196 111 29 / 14%);
  color: #8a4f1d;
}

.ds-card--harmony .ds-card__chip.is-ok {
  background: rgb(31 111 139 / 14%);
  color: #1f5d77;
}

.ds-card--honeypot .ds-card__chip.is-ok {
  background: rgb(184 130 28 / 14%);
  color: #6f4f12;
}

.ds-card--scc .ds-card__chip.is-ok {
  background: rgb(37 99 208 / 14%);
  color: #1e4aa0;
}

.ds-card__chip.is-warn {
  background: rgb(196 61 61 / 14%);
  color: #8a3434;
}

/* Reconnecting: amber, pulsing — the operator's login tab is open and we are
   re-probing until the session comes back. */
.ds-card__chip.is-reconnecting {
  background: rgb(214 158 46 / 18%);
  color: #8a6516;
  animation: ds-chip-pulse-amber 1.4s ease-in-out infinite;
}

.ds-card.is-reconnecting {
  border-color: rgb(214 158 46 / 50%);
  background: rgb(214 158 46 / 6%);
}

.ds-card__dot.reconnecting {
  background: #d69e2e;
  box-shadow: 0 0 0 3px rgb(214 158 46 / 22%);
  animation: ds-dot-pulse-amber 1.4s ease-in-out infinite;
}

@keyframes ds-chip-pulse-amber {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.55;
  }
}

@keyframes ds-dot-pulse-amber {
  0%,
  100% {
    box-shadow: 0 0 0 3px rgb(214 158 46 / 22%);
  }
  50% {
    box-shadow: 0 0 0 5px rgb(214 158 46 / 10%);
  }
}

.ds-card__count {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  margin-left: 4px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  background: rgb(var(--toolbox-panel-rgb) / 60%);
  color: var(--toolbox-text);
}

.ds-card__status {
  margin: 0;
  color: var(--toolbox-muted);
  font-size: 12px;
}

.ds-card__error {
  margin: 0;
  color: #b34b4b;
  font-size: 12px;
}

.ds-card__actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* Dark theme: variantes legibles de los colores informativos fijos */
[data-theme="dark"] .ds-card--mercury .ds-card__chip.is-ok {
  color: #c89bd6;
}
[data-theme="dark"] .ds-card--perfectmile .ds-card__chip.is-ok {
  color: #e0a858;
}
[data-theme="dark"] .ds-card--harmony .ds-card__chip.is-ok {
  color: #54b2cc;
}
[data-theme="dark"] .ds-card--honeypot .ds-card__chip.is-ok {
  color: #d6c382;
}
[data-theme="dark"] .ds-card--scc .ds-card__chip.is-ok {
  color: #82a9d8;
}
[data-theme="dark"] .ds-card__chip.is-warn {
  color: #e07b7b;
}
</style>
