<template>
  <div class="tb-text-field">
    <button
      type="button"
      class="tb-text-field__display"
      :id="inputId || undefined"
      :aria-label="ariaLabel || title || t('components.textEdit.editValue')"
      @click="openEditor"
    >
      <span class="tb-text-field__value" :class="{ 'is-empty': !modelValue }">
        {{ modelValue || placeholder || t("bootstrap.chipField.emptyPlaceholder") }}
      </span>
      <span class="tb-text-field__edit" aria-hidden="true">✎</span>
    </button>

    <Modal
      v-model="open"
      :title="title || t('bootstrap.chipField.title')"
      :eyebrow="eyebrow || ''"
      :max-width="680"
    >
      <p v-if="hintText" class="tb-text-field__hint">{{ hintText }}</p>
      <textarea
        ref="textareaEl"
        v-model="draft"
        class="tb-text-field__textarea"
        :placeholder="editorPlaceholder || placeholder || ''"
        rows="4"
        spellcheck="false"
        @keydown.enter.exact.prevent="save"
      ></textarea>

      <template #footer>
        <button type="button" class="tb-button tb-button--ghost" @click="cancel">
          {{ t("common.cancel") }}
        </button>
        <button type="button" class="tb-button tb-button--primary" @click="save">
          {{ t("common.ok") }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { nextTick, ref, watch } from "vue";
import Modal from "./Modal.vue";
import { useI18n } from "../i18n";

const props = defineProps({
  modelValue: { type: String, default: "" },
  placeholder: { type: String, default: "" },
  editorPlaceholder: { type: String, default: "" },
  inputId: { type: String, default: "" },
  ariaLabel: { type: String, default: "" },
  title: { type: String, default: "" },
  eyebrow: { type: String, default: "" },
  hintText: { type: String, default: "" },
});

const emit = defineEmits(["update:modelValue"]);

const { t } = useI18n();

const open = ref(false);
const draft = ref("");
const textareaEl = ref(null);

async function openEditor() {
  draft.value = props.modelValue || "";
  open.value = true;
  await nextTick();
  try {
    textareaEl.value?.focus();
    textareaEl.value?.select();
  } catch (_e) {
    /* ignore */
  }
}

function save() {
  emit("update:modelValue", draft.value.trim());
  open.value = false;
}

function cancel() {
  open.value = false;
}

watch(
  () => props.modelValue,
  () => {
    if (!open.value) draft.value = "";
  },
);
</script>

<style scoped>
.tb-text-field {
  width: 100%;
}

.tb-text-field__display {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 34px;
  padding: 6px 10px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 8px;
  background: rgb(var(--toolbox-panel-rgb) / 58%);
  color: var(--toolbox-text);
  cursor: pointer;
  text-align: left;
  overflow: hidden;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.tb-text-field__display:hover,
.tb-text-field__display:focus-visible {
  border-color: var(--toolbox-accent);
  box-shadow: 0 0 0 3px rgb(var(--toolbox-accent-rgb, 31 111 139) / 18%);
  outline: 0;
}

.tb-text-field__value {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}

.tb-text-field__value.is-empty {
  color: var(--toolbox-muted);
}

.tb-text-field__edit {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgb(var(--toolbox-border-rgb) / 40%);
  color: var(--toolbox-accent);
  font-size: 12px;
  font-weight: 800;
}

.tb-text-field__hint {
  margin: 0 0 12px;
  color: var(--toolbox-muted);
  font-size: 12.5px;
  line-height: 1.5;
}

.tb-text-field__textarea {
  width: 100%;
  min-height: 96px;
  padding: 10px 12px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 8px;
  background: rgb(var(--toolbox-panel-rgb) / 58%);
  color: var(--toolbox-text);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
  resize: vertical;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.tb-text-field__textarea:focus {
  outline: 0;
  border-color: var(--toolbox-accent);
  box-shadow: 0 0 0 3px rgb(var(--toolbox-accent-rgb, 31 111 139) / 18%);
}
</style>
