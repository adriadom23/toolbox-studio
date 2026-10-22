<template>
  <div
    class="tb-chip-field"
    :class="{ 'is-readonly': readonly, 'is-compact': isCompact, 'is-single': single }"
  >
    <button
      type="button"
      class="tb-chip-field__display"
      :id="inputId || undefined"
      :disabled="readonly"
      :aria-label="ariaLabel || title || t('components.chipList.chips')"
      @click="openEditor"
    >
      <span
        v-for="(tok, index) in visibleTokens"
        :key="`chip-${index}-${tok}`"
        class="tb-chip-field__chip"
      >
        <span class="tb-chip-field__chip-label">{{ mask ? "••••••••" : tok }}</span>
        <span
          v-if="!readonly"
          class="tb-chip-field__chip-remove"
          role="button"
          tabindex="-1"
          :aria-label="removeLabel ? removeLabel(tok) : t('components.chipList.remove', { token: tok })"
          @click.stop="removeAt(index)"
        >×</span>
      </span>
      <span
        v-if="hiddenCount > 0"
        class="tb-chip-field__more"
        :title="hiddenTokens.join(', ')"
      >+{{ hiddenCount }}</span>
      <span v-if="!tokens.length" class="tb-chip-field__empty">
        {{ placeholder || t("bootstrap.chipField.emptyPlaceholder") }}
      </span>
      <span v-if="!readonly" class="tb-chip-field__edit" aria-hidden="true">✎</span>
    </button>

    <Modal
      v-model="open"
      :title="title || t('bootstrap.chipField.title')"
      :eyebrow="eyebrow || ''"
      :max-width="620"
    >
      <p class="tb-chip-field__hint">{{ hintText || t("bootstrap.chipField.hint") }}</p>
      <!-- Lista cerrada de valores (p. ej. Tipo DSP 1.0/2.0, 2026-08-19):
           mismo chip por fuera, dropdown como editor del modal. -->
      <select
        v-if="single && options.length"
        v-model="draft"
        class="tb-chip-field__input"
      >
        <option value="">—</option>
        <option v-for="opt in options" :key="`opt-${opt}`" :value="opt">
          {{ opt }}
        </option>
      </select>
      <input
        v-else-if="single"
        v-model="draft"
        class="tb-chip-field__input"
        :type="mask ? 'password' : 'text'"
        :placeholder="editorPlaceholder || placeholder || ''"
        :autocomplete="mask ? 'new-password' : 'off'"
        spellcheck="false"
        @keydown.enter.prevent="save"
      />
      <TokenChipInput
        v-else
        v-model="draft"
        :token-pattern="tokenPattern"
        :upper="upper"
        :unique="dedupe === 'case-insensitive'"
        :placeholder="editorPlaceholder || placeholder || ''"
      />
      <p v-if="invalidTokens.length" class="tb-chip-field__error">
        {{
          invalidMessage
            ? invalidMessage(invalidTokens.join(", "))
            : t("bootstrap.chipField.invalid", { list: invalidTokens.join(", ") })
        }}
      </p>

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
import { computed, ref, watch } from "vue";
import Modal from "./Modal.vue";
import TokenChipInput from "./TokenChipInput.vue";
import { useI18n } from "../i18n";

const props = defineProps({
  modelValue: { type: String, default: "" },
  placeholder: { type: String, default: "" },
  inputId: { type: String, default: "" },
  ariaLabel: { type: String, default: "" },
  title: { type: String, default: "" },
  eyebrow: { type: String, default: "" },
  hintText: { type: String, default: "" },
  editorPlaceholder: { type: String, default: "" },
  // Token validation. Default matches any non-whitespace token.
  tokenPattern: { type: RegExp, default: () => /^\S+$/ },
  // Whether to uppercase every token on save.
  upper: { type: Boolean, default: false },
  // Dedupe strategy. "case-insensitive" | "case-sensitive" | "none".
  dedupe: { type: String, default: "case-insensitive" },
  removeLabel: { type: Function, default: null },
  invalidMessage: { type: Function, default: null },
  // Separator used to serialize the stored string. Kept as "," so
  // legacy schemas that read CSV keep working unchanged.
  joinWith: { type: String, default: "," },
  // When >0 the inline preview only shows this many chips and adds a
  // "+N" pill for the rest. The modal always shows every value.
  maxPreview: { type: Number, default: 0 },
  // Disables the modal editor (chips + remove hidden). Handy for
  // auto-generated read-only lists like ALLOWED_DSPS.
  readonly: { type: Boolean, default: false },
  // When >0 the field stores at most this many tokens (the last valid
  // ones win). Used for single-value fields that reuse the chip look,
  // like the Flow NS induct times.
  maxTokens: { type: Number, default: 0 },
  // Single-value mode: the WHOLE string is one chip (never split on
  // spaces/commas), the editor is a plain input and × clears the value.
  // Used to give every plain config input the chip look (2026-08-18)
  // without corrupting values that contain spaces (names, subjects...).
  single: { type: Boolean, default: false },
  // Masks the chip label (and the editor input) for secret values.
  mask: { type: Boolean, default: false },
  // Optional per-token transform applied BEFORE validation, on save and
  // on the inline invalid check (e.g. hour normalization "0800"/"8" →
  // "08:00" via shared/timeText.js): a normalizable draft never flags
  // as invalid and always persists normalized.
  transform: { type: Function, default: null },
  // Closed list of allowed values (single mode): the modal editor
  // renders a dropdown instead of the free input, keeping the chip
  // look identical to every other field (2026-08-19, Tipo DSP).
  options: { type: Array, default: () => [] },
});

const emit = defineEmits(["update:modelValue"]);

const { t } = useI18n();

const SEPARATORS = /[,;\s|]+/;

const open = ref(false);
const draft = ref("");

function splitTokens(raw) {
  if (!raw) return [];
  return String(raw)
    .split(SEPARATORS)
    .map((t) => t.trim())
    .filter(Boolean);
}

function normalize(value) {
  return props.upper ? value.toUpperCase() : value;
}

function applyTransform(value) {
  if (!props.transform) return value;
  try {
    return String(props.transform(value) ?? value);
  } catch {
    return value;
  }
}

const tokens = computed(() => {
  if (props.single) {
    const raw = String(props.modelValue ?? "").trim();
    return raw ? [normalize(raw)] : [];
  }
  return splitTokens(props.modelValue).map(normalize);
});

const isCompact = computed(() => props.maxPreview > 0);

const visibleTokens = computed(() => {
  if (!isCompact.value) return tokens.value;
  return tokens.value.slice(0, props.maxPreview);
});

const hiddenTokens = computed(() => {
  if (!isCompact.value) return [];
  return tokens.value.slice(props.maxPreview);
});

const hiddenCount = computed(() => hiddenTokens.value.length);

const invalidTokens = computed(() => {
  if (props.single) {
    const raw = applyTransform(draft.value.trim());
    return raw && !props.tokenPattern.test(raw) ? [raw] : [];
  }
  return splitTokens(draft.value)
    .map(applyTransform)
    .filter((t) => !props.tokenPattern.test(t));
});

function emitTokens(nextTokens) {
  const cleaned = [];
  const seen = new Set();
  for (const raw of nextTokens) {
    const value = normalize(applyTransform(raw));
    if (!value) continue;
    if (!props.tokenPattern.test(value)) continue;
    if (props.dedupe !== "none") {
      const dedupeKey = props.dedupe === "case-insensitive" ? value.toLowerCase() : value;
      if (seen.has(dedupeKey)) continue;
      seen.add(dedupeKey);
    }
    cleaned.push(value);
  }
  const limited =
    props.maxTokens > 0 ? cleaned.slice(-props.maxTokens) : cleaned;
  emit("update:modelValue", limited.join(props.joinWith));
}

function openEditor() {
  if (props.readonly) return;
  draft.value = props.single
    ? String(props.modelValue ?? "").trim()
    : tokens.value.join(", ");
  open.value = true;
}

function save() {
  if (props.single) {
    const raw = applyTransform(draft.value.trim());
    if (!raw) {
      emit("update:modelValue", "");
    } else if (props.tokenPattern.test(raw)) {
      emit("update:modelValue", normalize(raw));
    }
    // Invalid single values keep the previous stored value (the modal
    // already showed the inline error before OK).
    open.value = false;
    return;
  }
  emitTokens(splitTokens(draft.value));
  open.value = false;
}

function cancel() {
  open.value = false;
}

function removeAt(index) {
  if (props.single) {
    emit("update:modelValue", "");
    return;
  }
  const next = [...tokens.value];
  next.splice(index, 1);
  emitTokens(next);
}

// Keep the draft in sync with external changes while the editor is closed.
watch(
  () => props.modelValue,
  () => {
    if (!open.value) draft.value = "";
  },
);
</script>

<style scoped>
.tb-chip-field {
  width: 100%;
  /* Grid/flex parents give items an implicit min-width:auto, which lets a
   * long unbreakable value (URLs, webhooks) force this field — and the
   * whole card — wider than its column, painting over the neighbour card.
   * Capping here keeps every consumer contained without per-card fixes. */
  min-width: 0;
  max-width: 100%;
}

.tb-chip-field__display {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  min-height: 40px;
  padding: 6px 8px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 8px;
  background: rgb(var(--toolbox-panel-rgb) / 58%);
  color: var(--toolbox-text);
  cursor: pointer;
  text-align: left;
  overflow: hidden;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

/* Non-compact mode wraps naturally so the field grows with the number
 * of chips. Compact mode stays on a single line and relies on the
 * "+N" pill for overflow. */
.tb-chip-field:not(.is-compact) .tb-chip-field__display {
  flex-wrap: wrap;
}

.tb-chip-field.is-compact .tb-chip-field__display {
  flex-wrap: nowrap;
  min-width: 0;
}

/* Read-only chip lists are meant to be read at a glance, so let them
 * wrap even in compact mode. The "+N" pill still works when the parent
 * container is narrower than all chips combined. */
.tb-chip-field.is-readonly.is-compact .tb-chip-field__display {
  flex-wrap: wrap;
}

.tb-chip-field__display:hover,
.tb-chip-field__display:focus-visible {
  border-color: var(--toolbox-accent);
  box-shadow: 0 0 0 3px rgb(var(--toolbox-accent-rgb, 31 111 139) / 18%);
  outline: 0;
}

.tb-chip-field.is-readonly .tb-chip-field__display {
  cursor: default;
  background: rgb(var(--toolbox-panel-rgb) / 42%);
}

.tb-chip-field.is-readonly .tb-chip-field__display:hover,
.tb-chip-field.is-readonly .tb-chip-field__display:focus-visible {
  border-color: rgb(var(--toolbox-border-rgb) / 76%);
  box-shadow: none;
}

.tb-chip-field__chip {
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
  max-width: 100%;
  min-width: 0;
}

.tb-chip-field__chip-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 220px;
  /* Without this a nowrap label refuses to shrink below its content
   * (flex min-width:auto), so the ellipsis never kicks in. */
  min-width: 0;
}

.tb-chip-field__chip-label::before {
  content: "✓ ";
  color: rgb(75 143 92 / 100%);
  font-weight: 900;
}

.tb-chip-field__chip-remove {
  display: inline-grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgb(var(--toolbox-accent-rgb, 31 111 139) / 22%);
  color: var(--toolbox-accent);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.12s ease, transform 0.12s ease;
}

.tb-chip-field__chip-remove:hover {
  background: rgb(179 75 75 / 22%);
  color: rgb(179 75 75);
  transform: scale(1.05);
}

.tb-chip-field__empty {
  color: var(--toolbox-muted);
  font-size: 13px;
  font-family: ui-monospace, Consolas, monospace;
}

.tb-chip-field__edit {
  margin-left: auto;
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgb(var(--toolbox-border-rgb) / 40%);
  color: var(--toolbox-accent);
  font-size: 13px;
  font-weight: 800;
}

.tb-chip-field__more {
  display: inline-grid;
  place-items: center;
  min-width: 32px;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  background: rgb(var(--toolbox-border-rgb) / 45%);
  color: var(--toolbox-accent);
  font-size: 11.5px;
  font-weight: 800;
  cursor: help;
  flex: 0 0 auto;
}

/* Compact chips may shrink (labels ellipsize) so the row always fits
 * the card width and the "+N" pill and edit icon stay visible instead
 * of overflowing into the neighbouring card. */
.tb-chip-field.is-compact .tb-chip-field__chip {
  flex: 0 1 auto;
  min-width: 0;
}

.tb-chip-field.is-compact .tb-chip-field__chip-label {
  min-width: 0;
}

.tb-chip-field__chip-remove {
  flex: 0 0 auto;
}

/* Single-value chips hold whole URLs / names: one line, the label uses
 * the full row and ellipsizes, and the ×/✎ stay pinned inside the field.
 * The full value is only readable by opening the editor modal (same
 * pattern as the DSP email lists). */
.tb-chip-field.is-single .tb-chip-field__display {
  flex-wrap: nowrap;
}

.tb-chip-field.is-single .tb-chip-field__chip {
  flex: 0 1 auto;
  min-width: 0;
}

.tb-chip-field.is-single .tb-chip-field__chip-label {
  max-width: none;
}

/* Plain input used by the single-value modal editor. */
.tb-chip-field__input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 8px;
  background: rgb(var(--toolbox-panel-rgb) / 58%);
  color: var(--toolbox-text);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.tb-chip-field__input:focus {
  outline: 0;
  border-color: var(--toolbox-accent);
  box-shadow: 0 0 0 3px rgb(var(--toolbox-accent-rgb, 31 111 139) / 18%);
}

.tb-chip-field__hint {
  margin: 0 0 12px;
  color: var(--toolbox-muted);
  font-size: 12.5px;
  line-height: 1.5;
}

.tb-chip-field__error {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgb(179 75 75 / 12%);
  color: rgb(179 75 75);
  font-size: 12px;
}
</style>
