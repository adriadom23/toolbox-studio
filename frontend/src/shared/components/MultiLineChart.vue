<template>
  <div class="mline" :style="{ height: `${height}px` }">
    <svg
      v-if="hasData"
      ref="svgEl"
      role="img"
      :aria-label="ariaLabel || undefined"
      :viewBox="`0 0 ${viewWidth} ${viewHeight}`"
      preserveAspectRatio="xMidYMid meet"
      class="mline__svg"
      :style="{ height: `${viewHeight}px` }"
    >
      <!-- Horizontal grid + Y labels -->
      <g v-for="(tick, i) in yTicks" :key="`y-${i}`">
        <line
          :x1="padLeft"
          :x2="viewWidth - padRight"
          :y1="tick.y"
          :y2="tick.y"
          class="mline__grid"
        />
        <text :x="4" :y="tick.y + 3" class="mline__label">{{ tick.label }}</text>
      </g>

      <!-- One polyline per series -->
      <g v-for="(line, idx) in lines" :key="`line-${idx}`">
        <polyline
          v-if="line.segments.length"
          :points="line.polyline"
          :stroke="line.color"
          stroke-width="2.2"
          fill="none"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <circle
          v-for="(pt, p) in line.points"
          :key="`pt-${idx}-${p}`"
          :cx="pt.x"
          :cy="pt.y"
          r="3"
          :fill="line.color"
          stroke="#fff"
          stroke-width="1"
        />
      </g>

      <!-- X labels -->
      <text
        v-for="(lbl, i) in xLabelPoints"
        :key="`xl-${i}`"
        :x="lbl.x"
        :y="viewHeight - 4"
        text-anchor="middle"
        class="mline__label"
      >
        {{ lbl.label }}
      </text>
    </svg>
    <p v-else class="mline__empty">{{ emptyLabel || t("components.charts.noData") }}</p>

    <ul v-if="hasData && !hideLegend" class="mline__legend">
      <li v-for="(line, idx) in lines" :key="`lg-${idx}`">
        <span class="mline__swatch" :style="{ background: line.color }"></span>
        {{ line.name }}
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  // [{ name, color, values: [number|null, ...] }]
  series: { type: Array, default: () => [] },
  ariaLabel: { type: String, default: "" },
  // X-axis labels aligned to each index in `values`
  labels: { type: Array, default: () => [] },
  height: { type: Number, default: 220 },
  emptyLabel: { type: String, default: "" },
  // Hide the series legend (and reclaim its reserved vertical space). Opt-in
  // for single-series charts that have nothing meaningful to legend. Default
  // ``false`` keeps every existing consumer (DprDashboard, DnrsWeekly,
  // DiveDeep) byte-for-byte unaffected.
  hideLegend: { type: Boolean, default: false },
});

// Match the viewBox WIDTH to the SVG's real pixel width so text and points
// render 1:1 (no horizontal stretching on wide cards). The height is fixed
// from the `height` prop (minus room for the legend) and applied to the SVG
// explicitly, so it never collapses regardless of flex layout quirks.
const LEGEND_SPACE = 26;
const legendSpace = computed(() => (props.hideLegend ? 0 : LEGEND_SPACE));
const svgEl = ref(null);
const measuredWidth = ref(360);
let ro = null;

function _measure() {
  const el = svgEl.value;
  if (!el) return;
  const w = el.clientWidth || el.getBoundingClientRect().width;
  if (w > 0) measuredWidth.value = w;
}

// The <svg> lives behind a v-if (only when there's data), so it may appear
// after mount. Re-attach the observer whenever the element changes.
watch(
  svgEl,
  (el) => {
    if (ro) ro.disconnect();
    if (!el) return;
    _measure();
    if (typeof ResizeObserver !== "undefined") {
      ro = ro || new ResizeObserver(() => _measure());
      ro.observe(el);
    }
  },
  { flush: "post", immediate: true },
);

onBeforeUnmount(() => {
  if (ro) {
    ro.disconnect();
    ro = null;
  }
});

const viewWidth = computed(() => Math.max(measuredWidth.value, 120));
const viewHeight = computed(() => Math.max(props.height - legendSpace.value, 80));
const padLeft = 34;
const padRight = 10;
const padTop = 12;
const padBottom = 22;

const columns = computed(() => props.labels.length);

const hasData = computed(
  () =>
    columns.value >= 1 &&
    props.series.some((s) => (s.values || []).some((v) => typeof v === "number" && !Number.isNaN(v))),
);

const bounds = computed(() => {
  const values = [];
  for (const s of props.series) {
    for (const v of s.values || []) {
      if (typeof v === "number" && !Number.isNaN(v)) values.push(v);
    }
  }
  if (!values.length) return { min: 0, max: 1 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    const spread = Math.max(Math.abs(min) * 0.1, 0.5);
    return { min: min - spread, max: max + spread };
  }
  const pad = (max - min) * 0.1;
  return { min: min - pad, max: max + pad };
});

function yFromValue(value) {
  const { min, max } = bounds.value;
  const h = viewHeight.value - padTop - padBottom;
  if (max === min) return padTop + h / 2;
  return padTop + h * (1 - (value - min) / (max - min));
}

function xFromIndex(index) {
  const n = columns.value;
  if (n <= 1) return padLeft + (viewWidth.value - padLeft - padRight) / 2;
  const span = viewWidth.value - padLeft - padRight;
  return padLeft + (index / (n - 1)) * span;
}

const lines = computed(() =>
  props.series.map((s) => {
    const points = [];
    (s.values || []).forEach((v, idx) => {
      if (typeof v === "number" && !Number.isNaN(v)) {
        points.push({ x: xFromIndex(idx), y: yFromValue(v) });
      }
    });
    return {
      name: s.name,
      color: s.color || "#1d4ed8",
      points,
      segments: points,
      polyline: points.map((p) => `${p.x},${p.y}`).join(" "),
    };
  }),
);

const yTicks = computed(() => {
  const { min, max } = bounds.value;
  const mid = (max + min) / 2;
  return [
    { y: padTop, label: formatTick(max) },
    { y: yFromValue(mid), label: formatTick(mid) },
    { y: yFromValue(min), label: formatTick(min) },
  ];
});

function formatTick(value) {
  if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString();
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

const xLabelPoints = computed(() => {
  const n = columns.value;
  if (!n) return [];
  if (n <= 4) {
    return props.labels.map((label, idx) => ({ x: xFromIndex(idx), label }));
  }
  const indices = [0, Math.floor(n / 2), n - 1];
  return indices.map((idx) => ({ x: xFromIndex(idx), label: props.labels[idx] || "" }));
});
</script>

<style scoped>
.mline {
  position: relative;
  width: 100%;
  display: flex;
  flex-direction: column;
}

.mline__svg {
  width: 100%;
  flex: 0 0 auto;
  display: block;
}

.mline__grid {
  stroke: rgb(var(--toolbox-border-rgb) / 55%);
  stroke-dasharray: 3 3;
}

.mline__label {
  font-size: 9px;
  fill: rgb(var(--toolbox-text-rgb) / 78%);
  font-family: var(--tb-font-body);
}

.mline__empty {
  margin: 0;
  padding: 16px 0;
  text-align: center;
  color: var(--toolbox-muted);
  font-size: 12px;
}

.mline__legend {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 11px;
  color: var(--toolbox-muted);
}

.mline__legend li {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.mline__swatch {
  width: 12px;
  height: 3px;
  border-radius: 2px;
  display: inline-block;
}
</style>
