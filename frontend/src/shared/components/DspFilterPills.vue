<template>
  <div class="dsp-filter-pills" role="group">
    <span v-if="label" class="dsp-filter-pills__label">{{ label }}</span>
    <div class="dsp-filter-pills__row">
      <!-- "DS" = vista de estación completa (el sentinel "todas las DSP"
           de cada herramienta). Siempre presente y marcado por defecto. -->
      <button
        type="button"
        class="dsp-filter-pills__pill dsp-filter-pills__pill--ds"
        :class="{ 'is-selected': modelValue === allValue }"
        @click="select(allValue)"
      >
        {{ allLabel }}
      </button>
      <button
        v-for="dsp in dsps"
        :key="`pill-${dsp}`"
        type="button"
        class="dsp-filter-pills__pill"
        :class="{ 'is-selected': modelValue === dsp }"
        @click="select(dsp)"
      >
        {{ formatLabel ? formatLabel(dsp) : dsp }}
      </button>
    </div>
  </div>
</template>

<script setup>
// Selector de DSP por botones (sustituye a los <select> de DSP en las
// herramientas — instrucción del usuario 2026-08-29). Semántica de
// selector ÚNICO calcada de los pills OTRM/Flota de MetisPlan: pulsar
// una DSP la selecciona; re-pulsarla (o pulsar DS) vuelve a la vista
// de estación (allValue).
const props = defineProps({
  modelValue: { type: String, default: "" },
  dsps: { type: Array, default: () => [] },
  // Valor que representa "todas las DSP / estación" en la herramienta
  // anfitriona ("" en Daily, "ALL" en Dex/Dino/Dnrs/FtdsRts, "DS" en
  // DPR, "__all_dsps__" en UTL...).
  allValue: { type: String, default: "" },
  // "DS" es término de producto: sin i18n.
  allLabel: { type: String, default: "DS" },
  label: { type: String, default: "" },
  formatLabel: { type: Function, default: null },
});

const emit = defineEmits(["update:modelValue"]);

function select(value) {
  if (value !== props.allValue && props.modelValue === value) {
    emit("update:modelValue", props.allValue);
    return;
  }
  emit("update:modelValue", value);
}
</script>

<style scoped>
.dsp-filter-pills {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.dsp-filter-pills__label {
  font-size: 10.5px;
  font-weight: 800;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--toolbox-muted);
}
.dsp-filter-pills__row {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 8px;
}
.dsp-filter-pills__pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  border-radius: 999px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 56%);
  background: rgb(var(--toolbox-surface-rgb) / 50%);
  color: var(--toolbox-text);
  font: inherit;
  font-size: 12.5px;
  font-weight: 700;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.dsp-filter-pills__pill:hover {
  border-color: rgb(var(--toolbox-accent-rgb) / 45%);
}
.dsp-filter-pills__pill.is-selected {
  background: var(--toolbox-accent-soft);
  border-color: rgb(var(--toolbox-accent-rgb) / 50%);
  color: var(--toolbox-accent);
}
</style>
