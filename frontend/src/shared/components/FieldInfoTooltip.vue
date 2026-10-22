<template>
  <span class="tb-field-info-wrap">
    <button
      type="button"
      class="tb-field-info"
      :aria-label="ariaLabel || title || t('components.fieldInfo.help')"
      :aria-expanded="open ? 'true' : 'false'"
      @click.stop="show"
    >
      <slot>
        <span class="tb-field-info__glyph" aria-hidden="true">?</span>
      </slot>
    </button>

    <Modal
      v-model="open"
      :title="title || t('components.fieldInfo.help')"
      :eyebrow="eyebrow"
      :max-width="maxWidth"
      @close="onClose"
    >
      <div class="tb-field-info-modal">
        <slot name="body">
          <p v-if="text">{{ text }}</p>
        </slot>
      </div>
    </Modal>
  </span>
</template>

<script setup>
import { ref } from "vue";

import { useI18n } from "../i18n";
import Modal from "./Modal.vue";

const { t } = useI18n();

defineProps({
  title: { type: String, default: "" },
  eyebrow: { type: String, default: "" },
  text: { type: String, default: "" },
  ariaLabel: { type: String, default: "" },
  maxWidth: { type: [String, Number], default: 720 },
});

const emit = defineEmits(["open", "close"]);
const open = ref(false);

function show() {
  open.value = true;
  emit("open");
}

function onClose() {
  emit("close");
}
</script>

<style scoped>
.tb-field-info-wrap {
  display: inline-flex;
}

.tb-field-info {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 68%);
  border-radius: 50%;
  color: var(--toolbox-accent);
  background: rgb(var(--toolbox-panel-rgb) / 68%);
  font-weight: 800;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
  transition: border-color 0.12s ease, transform 0.1s ease, background 0.12s ease;
}

.tb-field-info:hover,
.tb-field-info:focus-visible {
  border-color: var(--toolbox-accent);
  background: rgb(var(--toolbox-panel-rgb) / 92%);
  transform: scale(1.06);
  outline: 0;
}

.tb-field-info__glyph {
  line-height: 1;
  transform: translateY(-0.5px);
}
</style>

<style>
/* Global (unscoped) styles for the modal body content. */
.tb-field-info-modal {
  font-family: var(--tb-font-body);
  font-size: 13.5px;
  line-height: 1.55;
  color: var(--toolbox-text);
}

.tb-field-info-modal p {
  margin: 0 0 12px;
}

.tb-field-info-modal p:last-child {
  margin-bottom: 0;
}

.tb-field-info-modal ol,
.tb-field-info-modal ul {
  margin: 0 0 14px;
  padding-left: 22px;
}

.tb-field-info-modal li {
  margin-bottom: 8px;
}

.tb-field-info-modal li:last-child {
  margin-bottom: 0;
}

.tb-field-info-modal a {
  color: var(--toolbox-accent);
  text-decoration: underline;
  text-underline-offset: 3px;
  word-break: break-word;
}

.tb-field-info-modal a:hover,
.tb-field-info-modal a:focus-visible {
  text-decoration-thickness: 2px;
}

.tb-field-info-modal figure {
  margin: 14px 0 4px;
}

.tb-field-info-modal figure:first-of-type {
  margin-top: 4px;
}

.tb-field-info-modal img {
  display: block;
  width: 100%;
  max-width: 100%;
  border-radius: 10px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 55%);
  box-shadow: 0 10px 26px rgb(var(--toolbox-shadow-rgb) / 18%);
}

.tb-field-info-modal figcaption {
  margin-top: 6px;
  font-size: 12px;
  color: var(--toolbox-muted);
  text-align: center;
  font-style: italic;
}

.tb-field-info-modal__link-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 0 4px;
}

.tb-field-info-modal__link-row .tb-field-info-modal__link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 999px;
  background: rgb(var(--toolbox-accent-rgb, 232 122 51) / 12%);
  border: 1px solid rgb(var(--toolbox-accent-rgb, 232 122 51) / 45%);
  color: var(--toolbox-accent);
  text-decoration: none;
  font-weight: 600;
  font-size: 12.5px;
  transition: background 0.12s ease, border-color 0.12s ease, transform 0.1s ease;
}

.tb-field-info-modal__link-row .tb-field-info-modal__link:hover,
.tb-field-info-modal__link-row .tb-field-info-modal__link:focus-visible {
  background: rgb(var(--toolbox-accent-rgb, 232 122 51) / 22%);
  border-color: var(--toolbox-accent);
  transform: translateY(-1px);
  outline: 0;
}

.tb-field-info-modal__hint {
  display: block;
  margin-top: 4px;
  color: var(--toolbox-muted);
  font-size: 12px;
  line-height: 1.4;
}

.tb-field-info-modal__steps li figure {
  margin-top: 10px;
}

.tb-field-info-modal__steps li + li {
  margin-top: 14px;
}
</style>
