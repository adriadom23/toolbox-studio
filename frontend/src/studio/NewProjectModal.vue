<template>
  <Modal :model-value="modelValue" :title="t('studio.newProject.title')" @update:model-value="onModalUpdate">
    <form class="new-project" @submit.prevent="submit">
      <label class="new-project__label" for="new-project-name">{{ t("studio.newProject.nameLabel") }}</label>
      <input
        id="new-project-name"
        ref="nameInput"
        v-model="name"
        type="text"
        class="new-project__input"
        :class="{ 'is-invalid': showError }"
        :placeholder="t('studio.newProject.namePlaceholder')"
        autocomplete="off"
        @input="serverError = ''"
      />
      <p v-if="slug" class="new-project__slug">{{ t("studio.newProject.slugPreview", { slug }) }}</p>
      <p class="new-project__rule" :class="{ 'is-invalid': showError }">
        {{ showError ? errorMessage : t("studio.newProject.rule") }}
      </p>
    </form>

    <template #footer>
      <button type="button" class="tb-button tb-button--ghost" @click="close">
        {{ t("studio.newProject.cancel") }}
      </button>
      <button
        type="button"
        class="tb-button tb-button--primary"
        :disabled="!slugValid || creating"
        @click="submit"
      >
        {{ creating ? t("studio.newProject.creating") : t("studio.newProject.create") }}
      </button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, nextTick, ref, watch } from "vue";

import { useI18n } from "../shared/i18n";
import Modal from "../shared/components/Modal.vue";
import { createProject } from "./api/studioHttp";

const { t } = useI18n();

const props = defineProps({
  modelValue: { type: Boolean, required: true },
});
const emit = defineEmits(["update:modelValue", "created"]);

const name = ref("");
const creating = ref(false);
const serverError = ref("");
const touched = ref(false);
const nameInput = ref(null);
const idempotencyKey = ref("");

function newKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `project-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// Mirrors backend scaffold.slugify + SLUG_RE (^[a-z][a-z0-9-]*$) exactly
// (design.md §6.2) so the inline preview/validation matches the server.
const MAX_SLUG_LEN = 40;
const SLUG_RE = /^[a-z][a-z0-9-]*$/;

const slug = computed(() =>
  name.value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, MAX_SLUG_LEN),
);

const slugValid = computed(() => SLUG_RE.test(slug.value));

const errorMessage = computed(() => {
  if (serverError.value) return serverError.value;
  return t("studio.newProject.invalid", { rule: t("studio.newProject.rule") });
});

// Only show the inline error once the user has typed something non-empty
// (or after a failed submit) — never on a pristine empty field.
const showError = computed(
  () => Boolean(serverError.value) || (touched.value && name.value.trim().length > 0 && !slugValid.value),
);

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      name.value = "";
      creating.value = false;
      serverError.value = "";
      touched.value = false;
      idempotencyKey.value = newKey();
      nextTick(() => nameInput.value?.focus());
    }
  },
);

watch(name, () => {
  touched.value = true;
});

function onModalUpdate(open) {
  emit("update:modelValue", open);
}

function close() {
  emit("update:modelValue", false);
}

async function submit() {
  touched.value = true;
  if (!slugValid.value || creating.value) return;
  creating.value = true;
  serverError.value = "";
  try {
    const meta = await createProject(name.value.trim(), idempotencyKey.value);
    emit("created", meta.name || slug.value);
  } catch (err) {
    if (err.status === 409) {
      serverError.value = t("studio.newProject.nameExists");
    } else {
      serverError.value = t("studio.newProject.createError", { error: err.message });
    }
  } finally {
    creating.value = false;
  }
}
</script>

<style scoped>
.new-project {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.new-project__label {
  font-size: 13px;
  font-weight: 700;
  color: var(--toolbox-text);
}

.new-project__input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 10px;
  background: rgb(var(--toolbox-panel-rgb) / 62%);
  color: var(--toolbox-text);
  font: inherit;
  font-size: 14px;
  box-sizing: border-box;
}

.new-project__input:focus-visible {
  outline: 2px solid var(--toolbox-accent);
  outline-offset: 1px;
}

.new-project__input.is-invalid {
  border-color: var(--toolbox-danger);
}

.new-project__slug {
  margin: 0;
  font-size: 12px;
  color: var(--toolbox-muted);
  font-family: var(--tb-font-mono, monospace);
}

.new-project__rule {
  margin: 0;
  font-size: 12px;
  color: var(--toolbox-muted);
}

.new-project__rule.is-invalid {
  color: var(--toolbox-danger);
  font-weight: 700;
}
</style>
