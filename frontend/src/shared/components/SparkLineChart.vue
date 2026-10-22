<template>
  <div class="spark" :style="{ height: `${height}px` }">
    <svg
      v-if="points.length >= 2"
      :viewBox="`0 0 ${viewWidth} ${viewHeight}`"
      preserveAspectRatio="none"
      class="spark__svg"
      role="img"
      :aria-label="ariaLabel || undefined"
      @mouseleave="hover = null"
    >
      <!-- Axis baselines -->
      <line
        v-for="(tick, i) in yTicks"
        :key="`y-${i}`"
        :x1="padLeft"
        :x2="viewWidth - padRight"
        :y1="tick.y"
        :y2="tick.y"
        class="spark__grid"
      />
      <!-- Threshold bands (T1/T2/T3) when provided -->
      <g v-if="thresholdBands.length">
        <rect
          v-for="(band, idx) in thresholdBands"
          :key="`band-${idx}`"
          :x="padLeft"
          :y="band.y"
          :width="viewWidth - padLeft - padRight"
          :height="band.height"
          :fill="band.color"
          opacity="0.18"
        />
      </g>
      <!-- Value line -->
      <polyline
        :points="polylinePoints"
        :stroke="strokeColor"
        stroke-width="2.4"
        fill="none"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <!-- Area fill under the line -->
      <polyline
        :points="areaPoints"
        :fill="strokeColor"
        opacity="0.08"
        stroke="none"
      />
      <!-- Dots + hover markers -->
      <g
        v-for="(pt, idx) in points"
        :key="`dot-${idx}`"
        @mouseenter="hover = idx"
      >
        <circle
          :cx="pt.x"
          :cy="pt.y"
          r="4"
          :fill="pt.color || strokeColor"
          stroke="#fff"
          stroke-width="1.2"
        />
        <rect
          :x="pt.x - 12"
          :y="padTop"
          :width="24"
          :height="viewHeight - padTop - padBottom"
          fill="transparent"
        />
      </g>
      <!-- Y labels (min/max) -->
      <text
        v-for="(tick, i) in yTicks"
        :key="`yl-${i}`"
        :x="4"
        :y="tick.y + 3"
        class="spark__label"
      >
        {{ tick.label }}
      </text>
      <!-- X labels -->
      <text
        v-for="(pt, idx) in xLabelPoints"
        :key="`xl-${idx}`"
        :x="pt.x"
        :y="viewHeight - 4"
        text-anchor="middle"
        class="spark__label"
      >
        {{ pt.label }}
      </text>
    </svg>
    <p v-else class="spark__empty">{{ emptyLabel || t("components.charts.noData") }}</p>

    <div v-if="hover !== null && points[hover]" class="spark__tooltip" :style="tooltipStyle">
      <strong>{{ points[hover].raw?.full_label || points[hover].raw?.week_label || "" }}</strong>
      <span>{{ points[hover].raw?.formatted ?? points[hover].raw?.value }}</span>
      <span v-if="points[hover].raw?.level" :style="{ color: points[hover].color }">
        {{ points[hover].raw.level }}
      </span>
      <span v-if="hoverExtra" class="spark__tooltip-extra">RC: {{ hoverExtra }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  series: { type: Array, default: () => [] },
  ariaLabel: { type: String, default: "" },
  color: { type: String, default: "#1d4ed8" },
  height: { type: Number, default: 140 },
  valueKey: { type: String, default: "value" },
  formattedKey: { type: String, default: "formatted" },
  emptyLabel: { type: String, default: "" },
  thresholds: {
    type: Object,
    default: () => ({}),
    // Expected shape: { higherIsBetter: bool, t1, t2, t3 }
  },
  // Optional extra tooltip line per point, keyed by the point's
  // full_label (fallback: week_label). Used by the DPR focus cards to
  // surface the RC recorded in the action plan saved for that week.
  extraTooltips: { type: Object, default: () => ({}) },
});

const viewWidth = 360;
const viewHeight = computed(() => props.height);
const padLeft = 32;
const padRight = 8;
const padTop = 12;
const padBottom = 20;

const hover = ref(null);

const strokeColor = computed(() => props.color || "#1d4ed8");

const cleanSeries = computed(() =>
  (props.series || []).filter(
    (item) => item && typeof item[props.valueKey] === "number" && !Number.isNaN(item[props.valueKey]),
  ),
);

const bounds = computed(() => {
  const values = cleanSeries.value.map((d) => d[props.valueKey]);
  if (!values.length) return { min: 0, max: 1 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    const spread = Math.max(Math.abs(min) * 0.1, 0.5);
    return { min: min - spread, max: max + spread };
  }
  const padY = (max - min) * 0.1;
  return { min: min - padY, max: max + padY };
});

function yFromValue(value) {
  const { min, max } = bounds.value;
  const h = viewHeight.value - padTop - padBottom;
  if (max === min) return padTop + h / 2;
  return padTop + h * (1 - (value - min) / (max - min));
}

function xFromIndex(index) {
  const series = cleanSeries.value;
  if (series.length <= 1) return padLeft + (viewWidth - padLeft - padRight) / 2;
  const span = viewWidth - padLeft - padRight;
  return padLeft + (index / (series.length - 1)) * span;
}

const points = computed(() =>
  cleanSeries.value.map((raw, idx) => ({
    x: xFromIndex(idx),
    y: yFromValue(raw[props.valueKey]),
    color: raw.level_color || strokeColor.value,
    raw,
  })),
);

const polylinePoints = computed(() => points.value.map((p) => `${p.x},${p.y}`).join(" "));

const areaPoints = computed(() => {
  if (!points.value.length) return "";
  const baseY = viewHeight.value - padBottom;
  const first = points.value[0];
  const last = points.value[points.value.length - 1];
  const mid = points.value.map((p) => `${p.x},${p.y}`).join(" ");
  return `${first.x},${baseY} ${mid} ${last.x},${baseY}`;
});

const yTicks = computed(() => {
  const { min, max } = bounds.value;
  return [
    { value: max, y: padTop, label: formatTick(max) },
    { value: (max + min) / 2, y: yFromValue((max + min) / 2), label: formatTick((max + min) / 2) },
    { value: min, y: yFromValue(min), label: formatTick(min) },
  ];
});

function formatTick(value) {
  if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString();
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

const xLabelPoints = computed(() => {
  const series = cleanSeries.value;
  if (!series.length) return [];
  const max = series.length;
  if (max <= 4) {
    return series.map((raw, idx) => ({
      x: xFromIndex(idx),
      label: raw.week_label || "",
    }));
  }
  const indices = [0, Math.floor(max / 2), max - 1];
  return indices.map((idx) => ({
    x: xFromIndex(idx),
    label: series[idx]?.week_label || "",
  }));
});

const thresholdBands = computed(() => {
  const cfg = props.thresholds || {};
  const { t1, t2, t3, higherIsBetter } = cfg;
  if ([t1, t2, t3].some((v) => v == null)) return [];
  const { min, max } = bounds.value;
  // Clamp thresholds inside the visible range to avoid giant bands.
  const clamp = (v) => Math.max(min, Math.min(max, v));
  if (higherIsBetter) {
    return [
      { y: yFromValue(max), height: yFromValue(clamp(t1)) - yFromValue(max), color: "#2ea84a" },
      { y: yFromValue(clamp(t1)), height: yFromValue(clamp(t2)) - yFromValue(clamp(t1)), color: "#f0a500" },
      { y: yFromValue(clamp(t2)), height: yFromValue(clamp(t3)) - yFromValue(clamp(t2)), color: "#d63b3b" },
    ].filter((band) => band.height > 0);
  }
  return [
    { y: yFromValue(max), height: yFromValue(clamp(t3)) - yFromValue(max), color: "#d63b3b" },
    { y: yFromValue(clamp(t3)), height: yFromValue(clamp(t2)) - yFromValue(clamp(t3)), color: "#f0a500" },
    { y: yFromValue(clamp(t2)), height: yFromValue(clamp(t1)) - yFromValue(clamp(t2)), color: "#2ea84a" },
  ].filter((band) => band.height > 0);
});

const hoverExtra = computed(() => {
  if (hover.value === null) return "";
  const raw = points.value[hover.value]?.raw;
  if (!raw) return "";
  const extras = props.extraTooltips || {};
  return extras[raw.full_label] || extras[raw.week_label] || "";
});

const tooltipStyle = computed(() => {
  if (hover.value === null || !points.value[hover.value]) return {};
  const pt = points.value[hover.value];
  const leftPct = (pt.x / viewWidth) * 100;
  return {
    left: `${Math.min(Math.max(leftPct, 5), 95)}%`,
    top: `${(pt.y / viewHeight.value) * 100}%`,
  };
});
</script>

<style scoped>
.spark {
  position: relative;
  width: 100%;
}

.spark__svg {
  width: 100%;
  height: 100%;
  display: block;
}

.spark__grid {
  stroke: rgb(var(--toolbox-shadow-rgb) / 10%);
  stroke-dasharray: 3 3;
}

.spark__label {
  font-size: 9px;
  fill: rgb(var(--toolbox-shadow-rgb) / 65%);
  font-family: var(--tb-font-body);
}

.spark__empty {
  margin: 0;
  padding: 16px 0;
  text-align: center;
  color: rgb(var(--toolbox-shadow-rgb) / 50%);
  font-size: 12px;
}

.spark__tooltip {
  position: absolute;
  pointer-events: none;
  transform: translate(-50%, calc(-100% - 8px));
  background: rgb(20 28 36 / 92%);
  color: #fff;
  font-size: 11px;
  padding: 6px 8px;
  border-radius: 6px;
  display: grid;
  gap: 2px;
  line-height: 1.3;
  white-space: nowrap;
  box-shadow: 0 6px 14px rgb(var(--toolbox-shadow-rgb) / 24%);
}

.spark__tooltip strong {
  font-size: 11px;
  font-weight: 800;
}

.spark__tooltip-extra {
  white-space: normal;
  max-width: 240px;
  font-size: 10.5px;
  color: #dbe4ee;
  border-top: 1px solid rgb(255 255 255 / 18%);
  padding-top: 3px;
  margin-top: 1px;
}
</style>
