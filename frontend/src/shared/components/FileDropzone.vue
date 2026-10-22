<template>
  <label
    class="tb-dropzone"
    :class="[
      {
        'is-over': isOver,
        'has-file': Boolean(modelValue) || showReady,
        'has-error': error,
        'is-preloaded': showReady,
        'is-loading': loading,
      },
      accent ? `tb-dropzone--${accent}` : '',
    ]"
    @dragenter.prevent="!loading && (isOver = true)"
    @dragover.prevent="!loading && (isOver = true)"
    @dragleave.prevent="isOver = false"
    @drop.prevent="handleDrop"
  >
    <input
      class="sr-only"
      :accept="accept"
      :disabled="loading || disabled"
      type="file"
      @change="handleInput"
    />
    <span class="tb-dropzone__content">
      <span v-if="loading" class="tb-dropzone__spinner" aria-hidden="true"></span>
      <span
        v-else-if="Boolean(modelValue) || showReady"
        class="tb-dropzone__check"
        aria-hidden="true"
      >✓</span>
      <strong class="tb-dropzone__title">{{ currentTitle }}</strong>
      <span v-if="currentMeta" class="tb-dropzone__meta">{{ currentMeta }}</span>
      <span v-if="loading" class="tb-dropzone__loading-track" aria-hidden="true">
        <span class="tb-dropzone__loading-bar"></span>
      </span>
      <span v-if="isPreloaded && !modelValue && replaceHint && !loading" class="tb-dropzone__hint">
        {{ replaceHint }}
      </span>
      <span v-if="error" class="tb-dropzone__meta tb-dropzone__meta--error">{{ error }}</span>
    </span>
  </label>
</template>

<script setup>
import { computed, ref } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  modelValue: {
    type: File,
    default: null,
  },
  accept: {
    type: String,
    default: "",
  },
  title: {
    type: String,
    required: true,
  },
  hint: {
    type: String,
    required: true,
  },
  selectedLabel: {
    type: String,
    required: true,
  },
  preloadedName: {
    type: String,
    default: "",
  },
  preloadedLabel: {
    type: String,
    default: "",
  },
  replaceHint: {
    type: String,
    default: "",
  },
  /**
   * Title shown while ``loading`` is true. When provided it overrides the
   * regular title so the slot can read e.g. "Downloading…" while an
   * automated source is filling it.
   */
  loadingLabel: {
    type: String,
    default: "",
  },
  /**
   * Optional preloaded-state tint. Match this to the data source colour:
   *   - "manifest" (green, default) for Plan Manifest
   *   - "mercury"  (purple) for Mercury queries
   *   - "perfectmile" (orange) for PerfectMile metrics
   *   - "harmony" (teal) for Harmony retrievals
   */
  accent: {
    type: String,
    default: "",
    validator: (value) => !value || ["manifest", "mercury", "perfectmile", "harmony"].includes(value),
  },
  /**
   * When true, the dropzone shows the bootstrap-style moving gradient
   * bar (matching the configuration loading effect) and disables the
   * file input. Used while an automated retrieval (PerfectMile DNR
   * fetch, Mercury auto-pull, ...) is filling the slot in the
   * background. Colour follows the ``accent`` prop.
   */
  loading: {
    type: Boolean,
    default: false,
  },
  /**
   * When true, the underlying file input is disabled — typically
   * because an automated source already populated the slot.
   */
  disabled: {
    type: Boolean,
    default: false,
  },
  /**
   * Force the "ready" (filled) visual — green tint + check mark —
   * without a preloaded file name. Used when an automated source
   * (e.g. Mercury rows) satisfies the slot but we don't want to show
   * a file name / row count in the meta line.
   */
  ready: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["update:modelValue"]);

const isOver = ref(false);
const error = ref("");

const isPreloaded = computed(() => Boolean(props.preloadedName) && !props.modelValue);

const showReady = computed(
  () => !props.modelValue && (isPreloaded.value || props.ready),
);

const currentTitle = computed(() => {
  if (props.loading && props.loadingLabel) return props.loadingLabel;
  if (props.modelValue) return props.selectedLabel;
  if (showReady.value) return props.preloadedLabel || props.selectedLabel;
  return props.title;
});

const currentMeta = computed(() => {
  if (props.modelValue) return props.modelValue.name;
  if (showReady.value) return props.preloadedName;
  return props.hint;
});

function validate(file) {
  if (!file) return false;
  if (!props.accept) return true;
  const allowed = props.accept
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
  const name = file.name.toLowerCase();
  return allowed.some((item) => name.endsWith(item));
}

function setFile(file) {
  if (!validate(file)) {
    error.value = t("components.fileDropzone.unsupportedFormat", { name: file?.name ?? "" });
    return;
  }
  error.value = "";
  emit("update:modelValue", file);
}

function handleDrop(event) {
  isOver.value = false;
  setFile(event.dataTransfer.files?.[0]);
}

function handleInput(event) {
  setFile(event.target.files?.[0]);
}
</script>

<style scoped>
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
}

.tb-dropzone__hint {
  margin-top: 6px;
  color: var(--toolbox-muted);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.02em;
}

.tb-dropzone.is-preloaded {
  border-style: solid;
  border-color: var(--toolbox-success);
  background: var(--toolbox-success-soft);
}

/* Per-source accent overrides for the preloaded tint. Order matters
 * for specificity — these come *after* the default rule so they win. */
.tb-dropzone.is-preloaded.tb-dropzone--manifest {
  border-color: #3c763d;
  background: rgb(60 118 61 / 12%);
}

.tb-dropzone.is-preloaded.tb-dropzone--mercury {
  border-color: #6b3a8a;
  background: rgb(110 67 145 / 14%);
}

.tb-dropzone.is-preloaded.tb-dropzone--perfectmile {
  border-color: #c46f1d;
  background: rgb(196 111 29 / 14%);
}

.tb-dropzone.is-preloaded.tb-dropzone--harmony {
  border-color: #1f6f8b;
  background: rgb(31 111 139 / 14%);
}

.tb-dropzone__meta--error {
  color: #b34b4b;
}

/* ---- Loading state ---------------------------------------------------
   Mirrors the bootstrap-loading-bar / stem-dropzone effect used in the
   configuration screens: a thin gradient bar slides across the bottom
   of the dropzone while an automated source (PerfectMile, Mercury,
   Harmony, ...) is filling the slot in the background. The accent
   colour follows the ``accent`` prop so the visual reads as
   "this source is working now". */
.tb-dropzone.is-loading {
  cursor: progress;
  border-color: var(--toolbox-accent);
  background: var(--toolbox-accent-soft);
}

.tb-dropzone__spinner {
  width: 18px;
  height: 18px;
  border-radius: 999px;
  border: 2px solid var(--toolbox-accent);
  border-top-color: transparent;
  display: inline-block;
  margin-bottom: 2px;
  animation: tb-dropzone-spin 0.7s linear infinite;
}

@keyframes tb-dropzone-spin {
  to { transform: rotate(360deg); }
}

.tb-dropzone__loading-track {
  display: block;
  margin-top: 6px;
  height: 4px;
  width: 100%;
  border-radius: 2px;
  background: rgb(var(--toolbox-accent-rgb) / 18%);
  overflow: hidden;
}

.tb-dropzone__loading-bar {
  display: block;
  width: 35%;
  height: 100%;
  background: linear-gradient(
    90deg,
    transparent,
    var(--toolbox-accent),
    transparent
  );
  animation: tb-dropzone-loading-bar 1.2s linear infinite;
}

@keyframes tb-dropzone-loading-bar {
  0% { transform: translateX(-110%); }
  100% { transform: translateX(310%); }
}

/* Per-source accent overrides for the loading bar so the colour
 * matches the data source the user is pulling from. */
.tb-dropzone.is-loading.tb-dropzone--mercury {
  border-color: #6b3a8a;
  background: rgb(110 67 145 / 12%);
}
.tb-dropzone.is-loading.tb-dropzone--mercury .tb-dropzone__spinner {
  border-color: #6b3a8a;
  border-top-color: transparent;
}
.tb-dropzone.is-loading.tb-dropzone--mercury .tb-dropzone__loading-track {
  background: rgb(110 67 145 / 16%);
}
.tb-dropzone.is-loading.tb-dropzone--mercury .tb-dropzone__loading-bar {
  background: linear-gradient(90deg, transparent, #6b3a8a, transparent);
}

.tb-dropzone.is-loading.tb-dropzone--perfectmile {
  border-color: #c46f1d;
  background: rgb(196 111 29 / 12%);
}
.tb-dropzone.is-loading.tb-dropzone--perfectmile .tb-dropzone__spinner {
  border-color: #c46f1d;
  border-top-color: transparent;
}
.tb-dropzone.is-loading.tb-dropzone--perfectmile .tb-dropzone__loading-track {
  background: rgb(196 111 29 / 16%);
}
.tb-dropzone.is-loading.tb-dropzone--perfectmile .tb-dropzone__loading-bar {
  background: linear-gradient(90deg, transparent, #c46f1d, transparent);
}

.tb-dropzone.is-loading.tb-dropzone--harmony {
  border-color: #1f6f8b;
  background: rgb(31 111 139 / 12%);
}
.tb-dropzone.is-loading.tb-dropzone--harmony .tb-dropzone__spinner {
  border-color: #1f6f8b;
  border-top-color: transparent;
}
.tb-dropzone.is-loading.tb-dropzone--harmony .tb-dropzone__loading-track {
  background: rgb(31 111 139 / 16%);
}
.tb-dropzone.is-loading.tb-dropzone--harmony .tb-dropzone__loading-bar {
  background: linear-gradient(90deg, transparent, #1f6f8b, transparent);
}
</style>
