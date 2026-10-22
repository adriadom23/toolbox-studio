<template>
  <div class="svg-chart">
    <svg :viewBox="`0 0 ${W} ${H}`" role="img">
      <!-- horizontal grid + y ticks -->
      <g v-for="tick in yTicks" :key="`y-${tick.value}`">
        <line class="grid" :x1="PAD_L" :x2="W - PAD_R" :y1="tick.y" :y2="tick.y" />
        <text class="tick" :x="PAD_L - 6" :y="tick.y + 3" text-anchor="end">{{ tick.label }}</text>
      </g>

      <!-- x labels -->
      <text
        v-for="lbl in xLabels"
        :key="`x-${lbl.index}`"
        class="tick"
        :x="lbl.x"
        :y="H - 6"
        text-anchor="middle"
      >{{ lbl.text }}</text>

      <!-- reference horizontal lines -->
      <g v-for="(hl, i) in hLineMarks" :key="`hl-${i}`">
        <line class="refline" :x1="PAD_L" :x2="W - PAD_R" :y1="hl.y" :y2="hl.y" />
        <text class="tick refline-label" :x="W - PAD_R" :y="hl.y - 4" text-anchor="end">{{ hl.label }}</text>
      </g>

      <!-- reference vertical lines -->
      <g v-for="(vl, i) in vLineMarks" :key="`vl-${i}`">
        <line class="refline refline--v" :x1="vl.x" :x2="vl.x" :y1="PAD_T" :y2="H - PAD_B" :style="{ stroke: vl.color }" />
        <text class="tick" :x="vl.x + 4" :y="PAD_T + 10" text-anchor="start" :style="{ fill: vl.color }">{{ vl.label }}</text>
      </g>

      <!-- series -->
      <g v-for="(s, i) in seriesMarks" :key="`s-${i}`">
        <path v-if="s.areaPath" class="area" :d="s.areaPath" :style="{ fill: s.color }" />
        <polyline
          class="line"
          :points="s.points"
          :style="{ stroke: s.color, strokeDasharray: s.dashArray }"
        />
      </g>
    </svg>
    <div class="legend">
      <span v-for="(s, i) in series" :key="`lg-${i}`">
        <i :style="{ background: s.color }"></i>{{ s.name }}
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

// Lightweight dependency-free SVG line/area chart (the project ships no
// charting library on purpose). Series share the categories axis.
const props = defineProps({
  /** X axis labels (one per data point). */
  categories: { type: Array, default: () => [] },
  /** [{ name, data:[Number], color, dash?: "dash"|"dot", area?: Boolean }] */
  series: { type: Array, default: () => [] },
  /** Horizontal reference lines: [{ value, label }]. */
  hLines: { type: Array, default: () => [] },
  /** Vertical reference lines: [{ index, label, color }]. */
  vLines: { type: Array, default: () => [] },
});

const W = 720;
const H = 250;
const PAD_L = 44;
const PAD_R = 10;
const PAD_T = 12;
const PAD_B = 24;

const count = computed(() =>
  Math.max(props.categories.length, ...props.series.map((s) => s.data?.length || 0), 2),
);

const maxY = computed(() => {
  let max = 0;
  for (const s of props.series) {
    for (const v of s.data || []) if (Number.isFinite(v) && v > max) max = v;
  }
  for (const hl of props.hLines) if (Number.isFinite(hl.value) && hl.value > max) max = hl.value;
  return max > 0 ? max * 1.06 : 1;
});

function xAt(i) {
  const iw = W - PAD_L - PAD_R;
  return PAD_L + (count.value <= 1 ? 0 : (i / (count.value - 1)) * iw);
}
function yAt(v) {
  const ih = H - PAD_T - PAD_B;
  const clamped = Math.max(0, Math.min(v, maxY.value));
  return PAD_T + ih - (clamped / maxY.value) * ih;
}

const yTicks = computed(() => {
  const ticks = [];
  for (let i = 0; i <= 4; i += 1) {
    const value = (maxY.value / 4) * i;
    ticks.push({ value, y: yAt(value), label: Math.round(value).toLocaleString() });
  }
  return ticks;
});

const xLabels = computed(() => {
  const total = props.categories.length;
  if (!total) return [];
  const step = Math.max(1, Math.ceil(total / 12));
  const out = [];
  for (let i = 0; i < total; i += step) {
    out.push({ index: i, x: xAt(i), text: props.categories[i] });
  }
  return out;
});

const seriesMarks = computed(() =>
  props.series.map((s) => {
    const data = s.data || [];
    const points = data.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
    let areaPath = "";
    if (s.area && data.length > 1) {
      const bottom = H - PAD_B;
      areaPath =
        `M ${xAt(0).toFixed(1)} ${bottom} ` +
        data.map((v, i) => `L ${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)}`).join(" ") +
        ` L ${xAt(data.length - 1).toFixed(1)} ${bottom} Z`;
    }
    return {
      points,
      areaPath,
      color: s.color || "var(--toolbox-accent)",
      dashArray: s.dash === "dash" ? "7 5" : s.dash === "dot" ? "2 4" : "none",
    };
  }),
);

const hLineMarks = computed(() =>
  props.hLines
    .filter((hl) => Number.isFinite(hl.value))
    .map((hl) => ({ y: yAt(hl.value), label: hl.label || "" })),
);

const vLineMarks = computed(() =>
  props.vLines
    .filter((vl) => Number.isFinite(vl.index) && vl.index >= 0)
    .map((vl) => ({
      x: xAt(vl.index),
      label: vl.label || "",
      color: vl.color || "var(--toolbox-muted)",
    })),
);
</script>

<style scoped>
.svg-chart svg {
  width: 100%;
  height: auto;
  display: block;
}
.grid {
  stroke: rgb(var(--toolbox-border-rgb) / 40%);
  stroke-width: 1;
}
.tick {
  fill: var(--toolbox-muted);
  font-size: 10px;
}
.refline {
  stroke: var(--toolbox-muted);
  stroke-width: 1;
  stroke-dasharray: 6 5;
  opacity: 0.8;
}
.refline--v {
  stroke-dasharray: 4 4;
}
.refline-label {
  font-weight: 700;
}
.line {
  fill: none;
  stroke-width: 2.2;
  vector-effect: non-scaling-stroke;
}
.area {
  opacity: 0.09;
  stroke: none;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 6px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--toolbox-muted);
}
.legend i {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 3px;
  margin-right: 5px;
  vertical-align: -1px;
}
</style>
