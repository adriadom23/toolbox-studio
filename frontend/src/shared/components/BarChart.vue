<template>
  <div class="bar-chart" :style="{ height: `${height}px` }">
    <svg
      v-if="rows.length"
      role="img"
      :aria-label="ariaLabel || undefined"
      :viewBox="`0 0 ${viewWidth} ${innerHeight}`"
      preserveAspectRatio="none"
      class="bar-chart__svg"
    >
      <g v-for="(row, idx) in rows" :key="`bar-${row.label}`">
        <text
          :x="0"
          :y="rowY(idx) + rowHeight / 2 + 4"
          class="bar-chart__label"
        >
          {{ row.label }}
        </text>
        <rect
          :x="barLeft"
          :y="rowY(idx) + 6"
          :width="barWidth(row.value)"
          :height="rowHeight - 12"
          :fill="row.color || strokeColor"
          rx="4"
        />
        <text
          :x="barLeft + barWidth(row.value) + 6"
          :y="rowY(idx) + rowHeight / 2 + 4"
          class="bar-chart__value"
        >
          {{ row.formatted ?? row.value }}
        </text>
      </g>
    </svg>
    <p v-else class="bar-chart__empty">{{ emptyLabel || t("components.charts.noData") }}</p>
  </div>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  rows: { type: Array, default: () => [] },
  ariaLabel: { type: String, default: "" },
  height: { type: Number, default: 220 },
  color: { type: String, default: "#1d4ed8" },
  emptyLabel: { type: String, default: "" },
  highlightLabel: { type: String, default: "" },
  highlightColor: { type: String, default: "#f0a500" },
  // Width (svg units) reserved for the label column; raise it when the
  // labels are long (e.g. "DA ID (DSP)") so they don't overlap the bars.
  labelWidth: { type: Number, default: 110 },
});

const viewWidth = 480;
const barLeft = computed(() => props.labelWidth + 6);
const valueColumnWidth = 56;
const rowGap = 4;

const strokeColor = computed(() => props.color);

const innerHeight = computed(() => {
  const minHeight = Math.max(props.height, 60);
  const computed = props.rows.length * 30 + rowGap * Math.max(props.rows.length - 1, 0);
  return Math.max(minHeight, computed);
});

const rowHeight = computed(() => {
  if (!props.rows.length) return 0;
  const usable = innerHeight.value;
  return usable / props.rows.length;
});

const maxValue = computed(() => {
  const positives = props.rows
    .map((r) => Math.abs(Number(r.value) || 0))
    .filter((v) => Number.isFinite(v));
  if (!positives.length) return 1;
  const max = Math.max(...positives, 1);
  return max;
});

function rowY(index) {
  return index * rowHeight.value;
}

function barWidth(value) {
  const span = viewWidth - barLeft.value - valueColumnWidth;
  if (!Number.isFinite(value)) return 0;
  const ratio = Math.abs(Number(value)) / maxValue.value;
  return Math.max(0, Math.min(1, ratio)) * span;
}
</script>

<style scoped>
.bar-chart {
  position: relative;
  width: 100%;
}

.bar-chart__svg {
  width: 100%;
  height: 100%;
  display: block;
}

.bar-chart__label {
  font-size: 11px;
  fill: var(--toolbox-text);
  font-family: var(--tb-font-body);
  font-weight: 700;
}

.bar-chart__value {
  font-size: 11px;
  fill: var(--toolbox-muted);
  font-family: var(--tb-font-body);
}

.bar-chart__empty {
  margin: 0;
  padding: 24px 0;
  text-align: center;
  color: var(--toolbox-muted);
  font-size: 12px;
}
</style>
