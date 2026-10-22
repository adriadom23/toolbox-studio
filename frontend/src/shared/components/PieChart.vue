<template>
  <div class="pie">
    <svg
      v-if="hasData"
      :viewBox="`0 0 ${size} ${size}`"
      class="pie__svg"
      role="img"
      :aria-label="ariaLabel || undefined"
      :style="{ width: `${height}px`, height: `${height}px` }"
    >
      <g v-if="slicesGeom.length === 1">
        <!-- A single slice can't be drawn as an arc: use a full ring. -->
        <circle
          :cx="center"
          :cy="center"
          :r="radius"
          fill="none"
          :stroke="slicesGeom[0].color"
          :stroke-width="ringWidth"
        />
      </g>
      <g v-else>
        <path
          v-for="slice in slicesGeom"
          :key="slice.label"
          :d="slice.path"
          :fill="slice.color"
        />
        <text
          v-for="slice in slicesGeom"
          v-show="slice.pct >= 6"
          :key="`pct-${slice.label}`"
          :x="slice.labelX"
          :y="slice.labelY"
          text-anchor="middle"
          dominant-baseline="middle"
          class="pie__slice-pct"
        >
          {{ slice.pct }}%
        </text>
      </g>
      <!-- Donut hole -->
      <circle :cx="center" :cy="center" :r="innerRadius" class="pie__hole" />
      <text :x="center" :y="center - 2" text-anchor="middle" class="pie__total">
        {{ totalValue.toLocaleString() }}
      </text>
      <text :x="center" :y="center + 14" text-anchor="middle" class="pie__total-label">
        {{ totalLabel }}
      </text>
    </svg>
    <p v-else class="pie__empty">{{ emptyLabel || t("components.charts.noData") }}</p>

    <ul v-if="hasData" class="pie__legend">
      <li v-for="slice in slicesGeom" :key="`lg-${slice.label}`">
        <span class="pie__swatch" :style="{ background: slice.color }"></span>
        <span class="pie__legend-label">{{ slice.label }}</span>
        <span class="pie__legend-value">
          {{ slice.value.toLocaleString() }} ({{ slice.pct }}%)
        </span>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  // [{ label, value, color? }]
  slices: { type: Array, default: () => [] },
  ariaLabel: { type: String, default: "" },
  height: { type: Number, default: 200 },
  totalLabel: { type: String, default: "Total" },
  emptyLabel: { type: String, default: "" },
});

const PALETTE = [
  "#1d4ed8",
  "#3c763d",
  "#c8783b",
  "#9b2d8f",
  "#0f9aa6",
  "#c23b3b",
  "#6b6f76",
  "#b59a18",
  "#5a4fcf",
  "#2f8f5b",
  "#d4663f",
  "#7a4fb0",
];

const size = 160;
const center = size / 2;
const radius = 64;
const ringWidth = 30;
const innerRadius = radius - ringWidth;

const normalized = computed(() =>
  props.slices
    .map((s, idx) => ({
      label: String(s.label ?? ""),
      value: Math.max(0, Number(s.value) || 0),
      color: s.color || PALETTE[idx % PALETTE.length],
    }))
    .filter((s) => s.value > 0),
);

const totalValue = computed(() =>
  normalized.value.reduce((acc, s) => acc + s.value, 0),
);

const hasData = computed(() => totalValue.value > 0);

function _polar(angle) {
  // angle in radians, 0 at 12 o'clock, clockwise.
  const a = angle - Math.PI / 2;
  return { x: center + radius * Math.cos(a), y: center + radius * Math.sin(a) };
}

const slicesGeom = computed(() => {
  const total = totalValue.value;
  if (!total) return [];
  const labelRadius = (radius + innerRadius) / 2;
  let acc = 0;
  return normalized.value.map((s) => {
    const startAngle = (acc / total) * 2 * Math.PI;
    acc += s.value;
    const endAngle = (acc / total) * 2 * Math.PI;
    const start = _polar(startAngle);
    const end = _polar(endAngle);
    const largeArc = endAngle - startAngle > Math.PI ? 1 : 0;
    const path = [
      `M ${center} ${center}`,
      `L ${start.x} ${start.y}`,
      `A ${radius} ${radius} 0 ${largeArc} 1 ${end.x} ${end.y}`,
      "Z",
    ].join(" ");
    const midAngle = (startAngle + endAngle) / 2 - Math.PI / 2;
    return {
      ...s,
      path,
      pct: Math.round((s.value / total) * 100),
      labelX: center + labelRadius * Math.cos(midAngle),
      labelY: center + labelRadius * Math.sin(midAngle),
    };
  });
});
</script>

<style scoped>
.pie {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  width: 100%;
}

.pie__svg {
  width: auto;
  max-width: 100%;
  aspect-ratio: 1 / 1;
  display: block;
}

.pie__hole {
  fill: var(--toolbox-surface);
}

.pie__slice-pct {
  font-size: 9px;
  font-weight: 800;
  fill: #fff;
  font-family: var(--tb-font-body);
  paint-order: stroke;
  stroke: rgb(20 28 36 / 45%);
  stroke-width: 2px;
}

.pie__total {
  font-size: 18px;
  font-weight: 800;
  fill: var(--toolbox-text);
  font-family: var(--tb-font-body);
}

.pie__total-label {
  font-size: 9px;
  fill: var(--toolbox-muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-family: var(--tb-font-body);
}

.pie__empty {
  margin: 0;
  padding: 24px 0;
  text-align: center;
  color: var(--toolbox-muted);
  font-size: 12px;
}

.pie__legend {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 4px 14px;
  width: 100%;
  font-size: 11px;
  color: var(--toolbox-text);
}

.pie__legend li {
  display: flex;
  align-items: center;
  gap: 6px;
}

.pie__swatch {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  flex: none;
}

.pie__legend-label {
  flex: 1;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pie__legend-value {
  color: var(--toolbox-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
</style>
