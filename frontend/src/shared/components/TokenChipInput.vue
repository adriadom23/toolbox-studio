<template>
  <div class="tb-chip-input" :class="{ 'is-focused': focused }">
    <span
      v-for="(token, index) in tokens"
      :key="`chip-${index}-${token}`"
      class="tb-chip-input__chip"
    >
      <span class="tb-chip-input__chip-label">{{ token }}</span>
      <button
        type="button"
        class="tb-chip-input__chip-remove"
        :aria-label="removeLabel ? removeLabel(token) : t('components.chipList.remove', { token })"
        @click="removeAt(index)"
      >×</button>
    </span>
    <input
      ref="inputEl"
      :id="inputId"
      v-model="draft"
      type="text"
      class="tb-chip-input__field"
      :placeholder="tokens.length ? '' : (placeholder || '')"
      :maxlength="maxLength || undefined"
      autocomplete="off"
      @keydown="onKeyDown"
      @blur="commitDraft(true)"
      @paste="onPaste"
      @focus="focused = true"
    />
  </div>
</template>

<script setup>
import { computed, ref } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  modelValue: { type: String, default: "" },
  placeholder: { type: String, default: "" },
  inputId: { type: String, default: "" },
  // Allowed regex per token. Tokens that don't match are dropped.
  tokenPattern: { type: RegExp, default: () => /^[A-Za-z0-9_-]{2,10}$/ },
  // Whether to uppercase every token on commit.
  upper: { type: Boolean, default: true },
  // Optional deduplication (case-insensitive).
  unique: { type: Boolean, default: true },
  maxLength: { type: Number, default: 0 },
  removeLabel: { type: Function, default: null },
});

const emit = defineEmits(["update:modelValue"]);

const draft = ref("");
const focused = ref(false);
const inputEl = ref(null);

const SEPARATORS = /[,;|\s]+/;

const tokens = computed(() => splitTokens(props.modelValue));

function splitTokens(raw) {
  if (!raw) return [];
  return String(raw)
    .split(SEPARATORS)
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => (props.upper ? t.toUpperCase() : t));
}

function isValid(token) {
  return props.tokenPattern.test(token);
}

function emitTokens(nextTokens) {
  const cleaned = [];
  const seen = new Set();
  for (const raw of nextTokens) {
    const value = props.upper ? raw.toUpperCase() : raw;
    if (!value) continue;
    if (!isValid(value)) continue;
    if (props.unique && seen.has(value.toUpperCase())) continue;
    cleaned.push(value);
    seen.add(value.toUpperCase());
  }
  emit("update:modelValue", cleaned.join(","));
}

function commitDraft(fromBlur = false) {
  focused.value = false;
  const raw = draft.value;
  if (!raw) {
    draft.value = "";
    return;
  }
  const parts = splitTokens(raw);
  if (parts.length) {
    const next = [...tokens.value, ...parts];
    emitTokens(next);
    draft.value = "";
    return;
  }
  if (fromBlur) draft.value = "";
}

function removeAt(index) {
  const next = [...tokens.value];
  next.splice(index, 1);
  emitTokens(next);
  inputEl.value?.focus();
}

function onKeyDown(event) {
  if (event.key === "Enter" || event.key === "," || event.key === ";" || event.key === "|") {
    event.preventDefault();
    commitDraft();
    return;
  }
  if (event.key === " " || event.key === "Tab") {
    // Space or Tab also commits, but let Tab keep navigating focus.
    if (draft.value.trim()) {
      commitDraft();
      if (event.key === "Tab") return; // allow default focus change
      event.preventDefault();
    }
    return;
  }
  if (event.key === "Backspace" && !draft.value && tokens.value.length) {
    event.preventDefault();
    removeAt(tokens.value.length - 1);
  }
}

function onPaste(event) {
  const text = (event.clipboardData || window.clipboardData)?.getData("text");
  if (!text) return;
  if (!SEPARATORS.test(text)) return;
  event.preventDefault();
  draft.value = "";
  emitTokens([...tokens.value, ...splitTokens(text)]);
}
</script>

<style scoped>
.tb-chip-input {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-height: 40px;
  padding: 6px 8px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 8px;
  background: rgb(var(--toolbox-panel-rgb) / 58%);
  cursor: text;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.tb-chip-input:hover,
.tb-chip-input.is-focused {
  border-color: var(--toolbox-accent);
  box-shadow: 0 0 0 3px rgb(var(--toolbox-accent-rgb, 31 111 139) / 18%);
}

.tb-chip-input__chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px 4px 10px;
  border-radius: 999px;
  background: rgb(var(--toolbox-accent-rgb, 31 111 139) / 15%);
  color: var(--toolbox-accent);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.3px;
  border: 1px solid rgb(var(--toolbox-accent-rgb, 31 111 139) / 40%);
}

.tb-chip-input__chip-label::before {
  content: "✓ ";
  color: rgb(75 143 92 / 100%);
  font-weight: 900;
}

.tb-chip-input__chip-remove {
  display: inline-grid;
  place-items: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: rgb(var(--toolbox-accent-rgb, 31 111 139) / 22%);
  color: var(--toolbox-accent);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.12s ease, transform 0.12s ease;
}

.tb-chip-input__chip-remove:hover,
.tb-chip-input__chip-remove:focus-visible {
  background: rgb(179 75 75 / 22%);
  color: rgb(179 75 75);
  outline: 0;
  transform: scale(1.05);
}

.tb-chip-input__field {
  flex: 1 1 120px;
  min-width: 100px;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--toolbox-text);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px;
  padding: 4px 2px;
}

.tb-chip-input__field::placeholder {
  color: var(--toolbox-muted);
}
</style>
