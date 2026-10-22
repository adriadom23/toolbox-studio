<template>
  <!-- Compact "Extraer" button for the CollapsedSection bar: icon-only
       (the section is minimized, so the button is too); the full label
       lives in the tooltip and aria-label for accessibility. -->
  <button
    type="button"
    class="tb-button tb-button--primary tb-extract-mini"
    :disabled="busy || disabled"
    :title="busy ? t('common.extracting') : tooltip"
    :aria-label="busy ? t('common.extracting') : tooltip"
    @click="$emit('extract')"
  >
    <span aria-hidden="true">📡</span>
  </button>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../i18n";

const props = defineProps({
  /** "none" | "partial" | "all" → drives the label, like IdentifyPanel. */
  extractState: {
    type: String,
    default: "none",
    validator: (v) => ["none", "partial", "all"].includes(v),
  },
  /** Number of sources behind the identity (label plural). */
  total: { type: Number, default: 1 },
  busy: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  /** Optional tooltip/aria-label override (e.g. "Actualizar desde STEM").
      When empty, the label derives from extractState like IdentifyPanel. */
  label: { type: String, default: "" },
});

defineEmits(["extract"]);

const { t } = useI18n();

const tooltip = computed(() => {
  if (props.label) return props.label;
  if (props.extractState === "all") return t("common.reextractAll");
  if (props.extractState === "partial") return t("common.extractRest");
  return props.total > 1 ? t("common.extractAll") : t("common.extract");
});
</script>

<style scoped>
.tb-extract-mini {
  padding: 5px 9px;
  font-size: 13px;
  line-height: 1;
}
</style>
