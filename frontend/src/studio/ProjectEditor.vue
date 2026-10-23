<template>
  <ComponentCatalog v-if="showCatalog" @back="showCatalog = false" />

  <div v-else class="project-editor">
    <header class="project-editor__bar">
      <div class="project-editor__bar-left">
        <button type="button" class="tb-button tb-button--ghost" @click="emit('back')">
          {{ t("studio.editor.back") }}
        </button>
        <h1 class="project-editor__name">{{ name }}</h1>
        <span v-if="dirty" class="project-editor__dirty">● {{ t("studio.editor.unsavedHint") }}</span>
      </div>
      <div class="project-editor__bar-right">
        <button type="button" class="tb-button tb-button--ghost" @click="load">
          ↻ {{ t("studio.editor.refreshFiles") }}
        </button>
        <button type="button" class="tb-button tb-button--ghost" @click="showCatalog = true">
          {{ t("studio.editor.componentLibrary") }}
        </button>
        <button type="button" class="tb-button tb-button--ghost" @click="openPreview">
          🔗 {{ t("studio.editor.openDevServer") }}
        </button>
        <button type="button" class="tb-button tb-button--primary" :disabled="saving" @click="save">
          {{ saving ? t("studio.editor.saving") : t("studio.editor.save") }}
        </button>
      </div>
    </header>

    <p v-if="savedFlash" class="project-editor__flash">{{ t("studio.editor.saved") }}</p>
    <p v-if="loadError" class="project-editor__error" role="alert">{{ loadError }}</p>
    <p v-if="saveError" class="project-editor__error" role="alert">{{ saveError }}</p>

    <div class="project-editor__body">
      <nav class="project-editor__tree" :aria-label="t('studio.editor.files')">
        <p class="project-editor__tree-title">{{ t("studio.editor.files") }}</p>
        <ul>
          <li
            v-for="file in files"
            :key="file.path"
            class="project-editor__file"
            :class="{ 'is-active': file.path === activePath, 'is-dirty': file.dirty, 'is-opaque': file.type === 'opaque' }"
          >
            <button type="button" @click="selectFile(file.path)">
              {{ file.path }}<span v-if="file.type === 'opaque'" class="project-editor__opaque-badge">{{ t("studio.editor.opaqueBadge") }}</span><span v-if="file.dirty" aria-hidden="true"> ●</span>
            </button>
          </li>
        </ul>
      </nav>

      <section class="project-editor__pane">
        <p v-if="!activeFile" class="project-editor__placeholder">{{ t("studio.editor.noFile") }}</p>
        <template v-else>
          <div v-if="activeFile.type === 'opaque'" class="project-editor__opaque" role="note">
            <strong>{{ t("studio.editor.opaqueTitle") }}</strong>
            <p>{{ t("studio.editor.opaqueDescription") }}</p>
            <dl>
              <div><dt>{{ t("studio.editor.opaqueType") }}</dt><dd>{{ activeFile.mediaType || "application/octet-stream" }}</dd></div>
              <div><dt>{{ t("studio.editor.opaqueSize") }}</dt><dd>{{ activeFile.size }} B</dd></div>
              <div><dt>SHA-256</dt><dd><code>{{ activeFile.sha256 }}</code></dd></div>
            </dl>
          </div>
          <template v-else>
            <p v-if="editorFallback" class="project-editor__fallback-note">
              {{ t("studio.editor.editorFallback") }}
            </p>
            <!-- CodeMirror mounts here when available. -->
            <div v-show="!editorFallback" ref="cmHost" class="project-editor__cm"></div>
            <!-- Plain-textarea fallback, feature-detected (design.md §5.3). -->
            <textarea
              v-if="editorFallback"
              v-model="fallbackText"
              class="project-editor__textarea"
              spellcheck="false"
              @input="onFallbackInput"
            ></textarea>
          </template>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import { useI18n } from "../shared/i18n";
import { showToolboxError } from "../shared/notifications/swal";
import { openProject, saveProjectFiles } from "./api/studioHttp";
import ComponentCatalog from "./ComponentCatalog.vue";

const { t } = useI18n();

const props = defineProps({
  name: { type: String, required: true },
});
const emit = defineEmits(["back", "dirty-change", "preview"]);

// Each file: { path, content (last-saved), draft (current buffer), dirty }.
const files = ref([]);
const activePath = ref("");
const loadError = ref("");
const saveError = ref("");
const saving = ref(false);
const savedFlash = ref(false);
const showCatalog = ref(false);

const cmHost = ref(null);
const editorFallback = ref(false);
const fallbackText = ref("");

let editorModule = null; // lazily-imported codemirrorSetup
let editorInstance = null; // { view, getValue }
let flashTimer = null;

// Computeds so the template (v-if="!activeFile", v-if="dirty") stays reactive.
const activeFile = computed(() => files.value.find((f) => f.path === activePath.value) || null);
const dirty = computed(() => files.value.some((f) => f.dirty));
// Internal helper (non-reactive lookup) used by the imperative buffer sync.
const currentFile = () => files.value.find((f) => f.path === activePath.value) || null;

async function load() {
  loadError.value = "";
  try {
    const data = await openProject(props.name);
    const list = Array.isArray(data.files) ? data.files : [];
    files.value = list.map((f) => ({
      path: f.path,
      type: f.type || "editable",
      mediaType: f.media_type || "",
      size: Number(f.size) || 0,
      content: f.type === "opaque" ? null : (f.content ?? ""),
      draft: f.type === "opaque" ? null : (f.content ?? ""),
      sha256: f.sha256 ?? null,
      dirty: false,
    }));
    // Prefer opening the .vue file first (the developer's main surface).
    const first =
      files.value.find((f) => f.path.toLowerCase().endsWith(".vue")) || files.value[0];
    if (first) await selectFile(first.path);
    else activePath.value = "";
  } catch (err) {
    loadError.value = t("studio.editor.loadError", { error: err.message });
  }
}

async function ensureEditorModule() {
  if (editorModule || editorFallback.value) return editorModule;
  try {
    editorModule = await import("./codemirrorSetup");
  } catch (err) {
    // CodeMirror bundle failed to load — degrade to the plain textarea.
    console.warn("[editor] CodeMirror unavailable, falling back to <textarea>", err);
    editorFallback.value = true;
    editorModule = null;
  }
  return editorModule;
}

function destroyEditor() {
  if (editorInstance) {
    try {
      editorInstance.view.destroy();
    } catch (_e) {
      /* already torn down */
    }
    editorInstance = null;
  }
}

async function selectFile(path) {
  // Persist the current buffer back into its file record before switching.
  syncActiveBuffer();
  activePath.value = path;
  const file = currentFile();
  if (!file) return;
  if (file.type === "opaque") {
    destroyEditor();
    return;
  }

  await ensureEditorModule();
  if (editorFallback.value) {
    fallbackText.value = file.draft;
    return;
  }

  await nextTick();
  destroyEditor();
  if (!cmHost.value) return;
  editorInstance = editorModule.createEditor({
    parent: cmHost.value,
    doc: file.draft,
    path: file.path,
    onChange: (text) => {
      file.draft = text;
      file.dirty = file.draft !== file.content;
    },
    onSave: () => save(),
  });
}

function syncActiveBuffer() {
  const file = currentFile();
  if (!file || file.type === "opaque") return;
  if (editorFallback.value) {
    file.draft = fallbackText.value;
  } else if (editorInstance) {
    file.draft = editorInstance.getValue();
  }
  file.dirty = file.draft !== file.content;
}

function onFallbackInput() {
  const file = currentFile();
  if (!file) return;
  file.draft = fallbackText.value;
  file.dirty = file.draft !== file.content;
}

async function sha256Text(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function save() {
  syncActiveBuffer();
  if (saving.value) return;
  saving.value = true;
  saveError.value = "";
  try {
    const dirtyFiles = files.value.filter((f) => f.type === "editable" && f.dirty);
    for (let start = 0; start < dirtyFiles.length; start += 100) {
      const batch = dirtyFiles.slice(start, start + 100);
      const payload = batch.map((f) => ({ path: f.path, content: f.draft, base_sha256: f.sha256 }));
      await saveProjectFiles(props.name, payload);
      await Promise.all(batch.map(async (file) => {
        file.content = file.draft;
        file.sha256 = await sha256Text(file.draft);
        file.dirty = false;
      }));
    }
    savedFlash.value = true;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => (savedFlash.value = false), 1800);
  } catch (err) {
    saveError.value = t("studio.editor.saveError", { error: err.message });
    showToolboxError({ title: t("studio.editor.save"), text: saveError.value });
  } finally {
    saving.value = false;
  }
}

function openPreview() {
  emit("preview");
}

watch(dirty, (value) => emit("dirty-change", value), { immediate: true });

onBeforeUnmount(() => {
  clearTimeout(flashTimer);
  destroyEditor();
});

load();
</script>

<style scoped>
.project-editor {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 68px);
}

.project-editor__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 20px;
  border-bottom: 1px solid rgb(var(--toolbox-border-rgb) / 55%);
}

.project-editor__bar-left,
.project-editor__bar-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.project-editor__name {
  margin: 0;
  font-size: 18px;
  color: var(--toolbox-text);
}

.project-editor__dirty {
  font-size: 12px;
  color: var(--toolbox-warning, #b8860b);
  font-weight: 700;
}

.project-editor__flash {
  margin: 0;
  padding: 6px 20px;
  color: #1f6f49;
  font-weight: 700;
  font-size: 13px;
}

.project-editor__error {
  margin: 0;
  padding: 6px 20px;
  color: var(--toolbox-danger);
  font-weight: 700;
  font-size: 13px;
}

.project-editor__body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 260px 1fr;
}

.project-editor__tree {
  border-right: 1px solid rgb(var(--toolbox-border-rgb) / 45%);
  padding: 12px;
  overflow: auto;
  background: rgb(var(--toolbox-surface-rgb) / 30%);
}

.project-editor__tree-title {
  margin: 0 0 8px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--toolbox-muted);
}

.project-editor__tree ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.project-editor__file button {
  width: 100%;
  text-align: left;
  padding: 6px 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--toolbox-text);
  font: inherit;
  font-size: 12.5px;
  font-family: var(--tb-font-mono, monospace);
  cursor: pointer;
  word-break: break-all;
}

.project-editor__file button:hover {
  background: rgb(var(--toolbox-border-rgb) / 40%);
}

.project-editor__file.is-active button {
  background: rgb(var(--toolbox-accent-rgb, 31 111 139) / 22%);
  color: var(--toolbox-text);
  font-weight: 700;
}

.project-editor__file.is-dirty button {
  color: var(--toolbox-warning, #b8860b);
}

.project-editor__opaque-badge {
  margin-left: 6px;
  color: var(--toolbox-muted);
  font-family: var(--toolbox-font, sans-serif);
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
}

.project-editor__opaque {
  margin: auto;
  max-width: 680px;
  padding: 24px;
  color: var(--toolbox-text);
}

.project-editor__opaque p { color: var(--toolbox-muted); }
.project-editor__opaque dl { display: grid; gap: 8px; }
.project-editor__opaque dl div { display: grid; gap: 2px; }
.project-editor__opaque dt { color: var(--toolbox-muted); font-size: 11px; font-weight: 700; text-transform: uppercase; }
.project-editor__opaque dd { margin: 0; overflow-wrap: anywhere; }

.project-editor__pane {
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.project-editor__placeholder {
  margin: auto;
  color: var(--toolbox-muted);
}

.project-editor__fallback-note {
  margin: 0;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--toolbox-warning, #b8860b);
  background: rgb(224 167 46 / 12%);
}

.project-editor__cm {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.project-editor__cm :deep(.cm-editor) {
  height: 100%;
}

.project-editor__cm :deep(.cm-scroller) {
  font-family: var(--tb-font-mono, monospace);
  font-size: 13px;
}

.project-editor__textarea {
  flex: 1;
  min-height: 0;
  width: 100%;
  padding: 12px;
  border: 0;
  resize: none;
  background: rgb(var(--toolbox-surface-rgb) / 20%);
  color: var(--toolbox-text);
  font-family: var(--tb-font-mono, monospace);
  font-size: 13px;
  box-sizing: border-box;
}
</style>
