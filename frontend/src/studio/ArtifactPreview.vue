<template>
  <section class="artifact-preview tool-surface" aria-labelledby="preview-title">
    <header>
      <div>
        <h1 id="preview-title">{{ t("studio.preview.title") }}</h1>
        <p id="preview-warning">{{ t("studio.preview.warning") }}</p>
        <small v-if="previewId">{{ t("studio.preview.metadata", { id: previewId }) }}</small>
      </div>
      <div class="artifact-preview__actions">
        <button
          v-if="!bootstrapUrl"
          ref="openButton"
          class="tb-button tb-button--primary"
          type="button"
          :disabled="loading"
          aria-describedby="preview-warning"
          @click="prepare"
        >
          {{ loading ? t("common.processing") : t("studio.preview.open") }}
        </button>
        <button v-else class="tb-button tb-button--ghost" type="button" @click="closePreview">
          {{ t("studio.preview.close") }}
        </button>
        <button v-if="loading && !bootstrapUrl" class="tb-button tb-button--ghost" type="button" @click="leaveBuild">
          {{ t("studio.preview.leave") }}
        </button>
      </div>
    </header>
    <p v-if="loading" role="status" aria-live="polite">{{ t("studio.preview.progress", { phase: buildPhase || "reserved" }) }}</p>
    <p v-if="error" class="artifact-preview__error" role="alert">{{ error }}</p>
    <div class="artifact-preview__frame-wrap">
      <iframe
        v-if="bootstrapUrl"
        ref="frame"
        :src="bootstrapUrl"
        :title="t('studio.preview.frameTitle')"
        sandbox="allow-scripts allow-same-origin"
        referrerpolicy="no-referrer"
        @load="sendGrant"
      ></iframe>
      <div v-else class="artifact-preview__placeholder" role="note">
        <strong>{{ t("studio.preview.consentTitle") }}</strong>
        <p>{{ t("studio.preview.empty") }}</p>
      </div>
    </div>
  </section>
</template>

<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from "vue";

import { postPreviewGrant, previewOrigin } from "../projects/_previewEntry";
import { useI18n } from "../shared/i18n";
import { buildWorkspacePreview, createPreviewGrant, getPreviewOperation, getPreviewStatus } from "./api/studioHttp";

const { t } = useI18n();
const props = defineProps({
  subjectKind: { type: String, required: true },
  subjectId: { type: String, required: true },
  previewId: { type: String, default: "" },
});
const frame = ref(null);
const openButton = ref(null);
const grant = ref(null);
const bootstrapUrl = ref("");
const loading = ref(false);
const error = ref("");
const buildPhase = ref("");
const availablePreviewId = ref("");
const activePreviewId = ref("");
let operationKey = "";
let requestGeneration = 0;

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const storageKey = () => `toolbox.preview-operation.${props.subjectId}`;

function previewOperationKey() {
  if (operationKey) return operationKey;
  try {
    operationKey = window.sessionStorage.getItem(storageKey()) || "";
  } catch (_reason) {
    operationKey = "";
  }
  if (!operationKey) {
    operationKey = globalThis.crypto?.randomUUID?.() || `preview-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    try {
      window.sessionStorage.setItem(storageKey(), operationKey);
    } catch (_reason) {
      // Session storage is optional; the in-memory key still prevents duplicate clicks.
    }
  }
  return operationKey;
}

function clearPreviewOperationKey() {
  operationKey = "";
  try {
    window.sessionStorage.removeItem(storageKey());
  } catch (_reason) {
    // Session storage is optional.
  }
}

function discardPreview({ restoreFocus = false, clearAvailable = false } = {}) {
  requestGeneration += 1;
  grant.value = null;
  bootstrapUrl.value = "";
  activePreviewId.value = "";
  loading.value = false;
  if (clearAvailable) availablePreviewId.value = "";
  if (restoreFocus) nextTick(() => openButton.value?.focus());
}

async function monitorWorkspaceSelection(generation, previewId) {
  while (generation === requestGeneration && bootstrapUrl.value && activePreviewId.value === previewId) {
    await delay(1000);
    if (generation !== requestGeneration || !bootstrapUrl.value) return;
    try {
      const status = await getPreviewStatus(previewId);
      if (!status.current || status.state !== "ready") {
        const nextPreviewId = status.current_preview_id || "";
        discardPreview();
        availablePreviewId.value = nextPreviewId;
        error.value = t("studio.preview.invalidated");
        nextTick(() => openButton.value?.focus());
        return;
      }
    } catch (_reason) {
      // A transient status error must not replace the already loaded preview.
    }
  }
}

async function prepare() {
  if (loading.value || bootstrapUrl.value) return;
  const generation = ++requestGeneration;
  loading.value = true;
  error.value = "";
  buildPhase.value = "reserved";
  try {
    let previewId = availablePreviewId.value || props.previewId;
    if (props.subjectKind === "workspace" && !previewId) {
      let operation = await buildWorkspacePreview(props.subjectId, previewOperationKey());
      const createdAt = Date.parse(operation.created_at || "");
      const startedAt = Number.isFinite(createdAt) ? createdAt : Date.now();
      const timeoutSeconds = Math.min(Math.max(Number(operation.timeout_seconds) || 900, 1), 900);
      const deadline = startedAt + timeoutSeconds * 1000;
      let interval = 250;
      while (["queued", "running"].includes(operation.state || operation.status)) {
        if (generation !== requestGeneration) return;
        buildPhase.value = operation.phase || buildPhase.value;
        if (Date.now() >= deadline) throw new Error("preview_poll_timeout");
        if (globalThis.document?.hidden) {
          await delay(1000);
          continue;
        }
        await delay(interval);
        if (generation !== requestGeneration) return;
        operation = await getPreviewOperation(operation.poll_url || operation.operation_id);
        interval = Math.min(Math.ceil(interval * 1.5), 5000);
      }
      if ((operation.state || operation.status) !== "ready") {
        clearPreviewOperationKey();
        throw new Error(operation.error_code || "preview_build_failed");
      }
      previewId = operation.preview_id;
      buildPhase.value = "ready";
      clearPreviewOperationKey();
    }
    const nextGrant = await createPreviewGrant({
      subjectKind: props.subjectKind,
      subjectId: props.subjectId,
      previewId,
    });
    if (generation !== requestGeneration) return;
    if (previewOrigin(nextGrant.bootstrap_url) === window.location.origin) {
      throw new Error(t("studio.preview.sameOrigin"));
    }
    grant.value = nextGrant;
    bootstrapUrl.value = nextGrant.bootstrap_url;
    activePreviewId.value = previewId;
    availablePreviewId.value = "";
    if (props.subjectKind === "workspace") void monitorWorkspaceSelection(generation, previewId);
  } catch (reason) {
    if (generation === requestGeneration) error.value = t("studio.preview.error", { error: reason.message });
  } finally {
    if (generation === requestGeneration) loading.value = false;
  }
}

function leaveBuild() {
  discardPreview({ restoreFocus: true });
}

function sendGrant() {
  if (!grant.value) return;
  const sent = postPreviewGrant(frame.value?.contentWindow, grant.value, {
    kind: props.subjectKind,
    id: props.subjectId,
  });
  if (sent) grant.value = null;
}

function closePreview() {
  discardPreview({ restoreFocus: true, clearAvailable: true });
}

watch(() => [props.subjectKind, props.subjectId, props.previewId], () => {
  error.value = "";
  buildPhase.value = "";
  operationKey = "";
  discardPreview({ clearAvailable: true });
});

onBeforeUnmount(() => discardPreview());
</script>

<style scoped>
.artifact-preview { max-width: 1320px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }
.artifact-preview header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.artifact-preview__actions { display: flex; gap: 8px; }
h1 { margin: 0; font-size: 26px; }
p { margin: 5px 0 0; color: var(--toolbox-muted); }
small { display: block; margin-top: 6px; color: var(--toolbox-muted); overflow-wrap: anywhere; }
.artifact-preview__error { color: var(--toolbox-danger); font-weight: 700; }
.artifact-preview__frame-wrap { min-height: 480px; display: grid; place-items: center; overflow: hidden; border: 1px solid var(--toolbox-border); border-radius: 12px; background: var(--toolbox-card); }
.artifact-preview__placeholder { max-width: 620px; padding: 24px; text-align: center; }
iframe { width: 100%; height: min(72vh, 820px); border: 0; }
@media (max-width: 600px) { .artifact-preview { padding: 14px; } .artifact-preview header { flex-direction: column; } .artifact-preview__frame-wrap { min-height: 360px; } }
</style>
