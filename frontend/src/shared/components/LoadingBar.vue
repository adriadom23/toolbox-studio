<template>
  <div
    v-if="active"
    class="tb-loading"
    :class="[`tb-loading--${variant}`, { 'tb-loading--indeterminate': isIndeterminate }]"
    role="progressbar"
    :aria-valuenow="displayPercent"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-label="label || undefined"
  >
    <div class="tb-loading__header" v-if="label || showPercent || sublabel">
      <span v-if="label" class="tb-loading__label">{{ label }}</span>
      <span v-if="sublabel" class="tb-loading__sublabel">{{ sublabel }}</span>
      <span v-if="showPercent && !isIndeterminate" class="tb-loading__percent">
        {{ displayPercent }}%
      </span>
    </div>
    <div class="tb-loading__track">
      <div
        class="tb-loading__fill"
        :style="fillStyle"
      />
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  active: {
    type: Boolean,
    default: true,
  },
  /** 0-100. If null/undefined, bar runs in indeterminate mode. */
  percent: {
    type: [Number, null],
    default: null,
  },
  label: {
    type: String,
    default: "",
  },
  sublabel: {
    type: String,
    default: "",
  },
  showPercent: {
    type: Boolean,
    default: true,
  },
  /** "inline" (compact) or "panel" (standalone card). */
  variant: {
    type: String,
    default: "inline",
    validator: (value) => ["inline", "panel"].includes(value),
  },
});

const isIndeterminate = computed(
  () => props.percent === null || props.percent === undefined || Number.isNaN(Number(props.percent)),
);

const displayPercent = computed(() => {
  if (isIndeterminate.value) return 0;
  const value = Number(props.percent);
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
});

const fillStyle = computed(() => {
  if (isIndeterminate.value) return {};
  return { width: `${displayPercent.value}%` };
});
</script>

<style scoped>
.tb-loading {
  display: grid;
  gap: 8px;
  width: 100%;
}

.tb-loading--panel {
  padding: 14px 16px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 56%);
  border-radius: 10px;
  background: rgb(var(--toolbox-surface-rgb) / 62%);
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

.tb-loading__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.tb-loading__label {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--toolbox-text);
}

.tb-loading__sublabel {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--toolbox-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tb-loading__percent {
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--toolbox-accent);
}

.tb-loading__track {
  position: relative;
  width: 100%;
  height: 8px;
  border-radius: 999px;
  background: rgb(var(--toolbox-border-rgb) / 55%);
  overflow: hidden;
}

.tb-loading__fill {
  position: absolute;
  inset: 0 auto 0 0;
  width: 0;
  border-radius: 999px;
  background: linear-gradient(
    90deg,
    var(--toolbox-accent),
    var(--toolbox-accent-hover)
  );
  box-shadow: 0 0 12px rgb(60 118 61 / 30%);
  transition: width 0.25s cubic-bezier(0.4, 0.1, 0.2, 1);
}

.tb-loading--indeterminate .tb-loading__fill {
  width: 40%;
  animation: tb-loading-slide 1.2s cubic-bezier(0.4, 0.1, 0.2, 1) infinite;
}

@keyframes tb-loading-slide {
  0% {
    transform: translateX(-120%);
  }
  60% {
    transform: translateX(180%);
  }
  100% {
    transform: translateX(180%);
  }
}

/* Variant: panel has a bit more breathing room */
.tb-loading--panel .tb-loading__track {
  height: 10px;
}
</style>
