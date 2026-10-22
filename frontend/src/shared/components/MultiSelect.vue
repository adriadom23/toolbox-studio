<template>
  <div
    ref="container"
    class="tb-multiselect"
    :class="{ open: isOpen, disabled }"
    @keydown.escape="close"
  >
    <button
      type="button"
      class="tb-multiselect__trigger tb-input"
      :disabled="disabled"
      :aria-expanded="isOpen"
      aria-haspopup="listbox"
      @click="toggle"
    >
      <span class="tb-multiselect__display">
        {{ displayText }}
      </span>
      <span class="tb-multiselect__chevron" aria-hidden="true">▾</span>
    </button>

    <div v-if="isOpen" class="tb-multiselect__dropdown" role="listbox" aria-multiselectable="true">
      <label
        v-for="option in primaryOptions"
        :key="option"
        class="tb-multiselect__option"
        :class="{ selected: isSelected(option) }"
      >
        <input
          type="checkbox"
          :checked="isSelected(option)"
          @change="toggleOption(option)"
        />
        <span>{{ option }}</span>
      </label>
      <div
        v-if="primaryOptions.length && secondaryOptions.length"
        class="tb-multiselect__separator"
        aria-hidden="true"
      ></div>
      <label
        v-for="option in secondaryOptions"
        :key="option"
        class="tb-multiselect__option tb-multiselect__option--offduty"
        :class="{ selected: isSelected(option) }"
      >
        <input
          type="checkbox"
          :checked="isSelected(option)"
          @change="toggleOption(option)"
        />
        <span>{{ option }}</span>
      </label>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  modelValue: {
    type: String,
    default: "",
  },
  options: {
    type: Array,
    default: () => [],
  },
  disabled: {
    type: Boolean,
    default: false,
  },
  placeholder: {
    type: String,
    default: "",
  },
  hideNa: {
    type: Boolean,
    default: false,
  },
  // Logins "en turno" (rota del día): se pintan primero y el resto de
  // opciones queda debajo de un separador (instrucción 2026-08-16).
  // Vacío = sin separador (comportamiento histórico).
  onDuty: {
    type: Array,
    default: () => [],
  },
});

const emit = defineEmits(["update:modelValue"]);

const isOpen = ref(false);
const container = ref(null);

const NA_OPTION = "N/A";

// Build options list: include "N/A" unless hidden + unique items from props.options
const allOptions = computed(() => {
  const unique = [...new Set(props.options.filter((o) => o && o.trim()))];
  if (props.hideNa) return unique;
  // Always put N/A first
  if (!unique.includes(NA_OPTION)) {
    return [NA_OPTION, ...unique];
  }
  return [NA_OPTION, ...unique.filter((o) => o !== NA_OPTION)];
});

// Split the options at the on-duty boundary: rota first (plus N/A), the
// rest after the separator. With no ``onDuty`` everything is primary.
const _onDutySet = computed(
  () => new Set((props.onDuty || []).map((o) => String(o || "").trim().toLowerCase()).filter(Boolean)),
);

const primaryOptions = computed(() => {
  if (!_onDutySet.value.size) return allOptions.value;
  return allOptions.value.filter(
    (o) => o === NA_OPTION || _onDutySet.value.has(o.trim().toLowerCase()),
  );
});

const secondaryOptions = computed(() => {
  if (!_onDutySet.value.size) return [];
  return allOptions.value.filter(
    (o) => o !== NA_OPTION && !_onDutySet.value.has(o.trim().toLowerCase()),
  );
});

// Parse model value (comma-separated) into a Set
const selectedSet = computed(() => {
  if (!props.modelValue || !props.modelValue.trim()) return new Set();
  return new Set(
    props.modelValue
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
});

const displayText = computed(() => {
  if (selectedSet.value.size === 0) {
    return props.placeholder || t("components.multiSelect.placeholder");
  }
  return Array.from(selectedSet.value).join(", ");
});

function isSelected(option) {
  return selectedSet.value.has(option);
}

function toggleOption(option) {
  const current = new Set(selectedSet.value);

  if (option === NA_OPTION) {
    // If selecting N/A, clear everything else
    if (current.has(NA_OPTION)) {
      current.delete(NA_OPTION);
    } else {
      current.clear();
      current.add(NA_OPTION);
    }
  } else {
    // If selecting a person, remove N/A
    current.delete(NA_OPTION);
    if (current.has(option)) {
      current.delete(option);
    } else {
      current.add(option);
    }
    // If nothing is selected after removing, default to N/A
    if (current.size === 0) {
      current.add(NA_OPTION);
    }
  }

  emit("update:modelValue", Array.from(current).join(", "));
}

function toggle() {
  if (props.disabled) return;
  isOpen.value = !isOpen.value;
}

function close() {
  isOpen.value = false;
}

function handleClickOutside(event) {
  if (container.value && !container.value.contains(event.target)) {
    close();
  }
}

onMounted(() => {
  document.addEventListener("mousedown", handleClickOutside);
});

onBeforeUnmount(() => {
  document.removeEventListener("mousedown", handleClickOutside);
});
</script>

<style scoped>
.tb-multiselect {
  position: relative;
  width: 100%;
}

.tb-multiselect.disabled {
  opacity: 0.55;
  pointer-events: none;
}

.tb-multiselect__trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 8px;
  cursor: pointer;
  text-align: left;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 56%);
  border-radius: 8px;
  padding: 8px 12px;
  background: rgb(var(--toolbox-surface-rgb) / 52%);
  color: var(--toolbox-text);
  font: inherit;
  font-size: 13px;
  min-height: 38px;
  transition: border-color 0.15s ease;
}

.tb-multiselect__trigger:hover:not(:disabled) {
  border-color: var(--toolbox-accent);
}

.tb-multiselect__trigger:disabled {
  cursor: not-allowed;
}

.tb-multiselect__display {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tb-multiselect__chevron {
  font-size: 12px;
  color: var(--toolbox-muted);
  transition: transform 0.15s ease;
}

.tb-multiselect.open .tb-multiselect__chevron {
  transform: rotate(180deg);
}

.tb-multiselect__dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  z-index: 100;
  max-height: 220px;
  overflow-y: auto;
  padding: 6px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 66%);
  border-radius: 10px;
  background: var(--toolbox-panel-bg, rgb(var(--toolbox-panel-rgb) / 96%));
  box-shadow: 0 12px 32px rgb(var(--toolbox-shadow-rgb) / 18%);
  animation: tbMultiselectFade 0.12s ease-out;
}

.tb-multiselect__option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: var(--toolbox-text);
  transition: background 0.1s ease;
}

.tb-multiselect__option:hover {
  background: rgb(var(--toolbox-border-rgb) / 28%);
}

.tb-multiselect__option.selected {
  background: rgb(var(--toolbox-accent-rgb, 56 178 172) / 12%);
  font-weight: 600;
}

.tb-multiselect__separator {
  margin: 6px 4px;
  border-top: 1px dashed rgb(var(--toolbox-border-rgb) / 80%);
}

.tb-multiselect__option--offduty {
  opacity: 0.78;
}

.tb-multiselect__option input[type="checkbox"] {
  accent-color: var(--toolbox-accent);
  width: 15px;
  height: 15px;
  cursor: pointer;
}

@keyframes tbMultiselectFade {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
