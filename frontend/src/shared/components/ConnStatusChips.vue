<template>
  <!-- Read-only per-source status chips for the CollapsedSection bar:
       same look as the IdentifyPanel chips (green connected / red
       disconnected / amber reconnecting) but without the inline
       "Conectar" action — the bar only reports state. -->
  <span class="tb-conn-chips">
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
    </span>
  </span>
</template>

<script setup>
import { useI18n } from "../i18n";

const props = defineProps({
  /** useIdentity(...) instance owning the sources to report. */
  identity: { type: Object, required: true },
});

const { t } = useI18n();

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
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid rgb(var(--toolbox-border-rgb, 31 111 139) / 55%);
  background: rgb(var(--toolbox-surface-rgb, 255 255 255) / 60%);
  font-size: 11.5px;
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
  animation: tb-chip-pulse 1.3s ease-in-out infinite;
}

@keyframes tb-chip-pulse {
  0%,
  100% {
    box-shadow: 0 0 0 3px rgb(224 167 46 / 22%);
  }
  50% {
    box-shadow: 0 0 0 6px rgb(224 167 46 / 6%);
  }
}
</style>
