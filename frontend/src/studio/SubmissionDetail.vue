<template>
  <section class="submission-detail tool-surface">
    <button class="tb-button tb-button--ghost" type="button" @click="emit('back')">← {{ t("studio.detail.back") }}</button>
    <p v-if="loading && !submission" role="status">{{ t("common.processing") }}</p>
    <div v-if="error" class="submission-detail__error" role="alert">
      {{ error }} <button type="button" @click="load">{{ t("studio.common.retry") }}</button>
    </div>

    <template v-if="submission">
      <header class="submission-detail__head">
        <div>
          <div class="submission-detail__eyebrow">{{ submission.project_slug }} · {{ submission.version_label }}</div>
          <h1>{{ submission.title }}</h1>
          <p>{{ submission.description }}</p>
        </div>
        <SubmissionStatusBadge :status="submission.state" />
      </header>

      <dl class="submission-detail__meta">
        <div><dt>{{ t("studio.detail.author") }}</dt><dd>{{ submission.author_display || submission.author_id }}</dd></div>
        <div><dt>{{ t("studio.detail.created") }}</dt><dd>{{ formatDate(submission.created_at) }}</dd></div>
        <div><dt>{{ t("studio.detail.updated") }}</dt><dd>{{ formatDate(submission.updated_at) }}</dd></div>
        <div><dt>SHA-256</dt><dd><code>{{ submission.artifact_sha256 || "—" }}</code></dd></div>
      </dl>

      <section class="submission-detail__panel" aria-labelledby="build-title">
        <h2 id="build-title">{{ t("studio.detail.build") }}</h2>
        <dl class="submission-detail__meta submission-detail__meta--nested">
          <div><dt>{{ t("studio.detail.provider") }}</dt><dd>{{ submission.build?.provider || "—" }}</dd></div>
          <div><dt>{{ t("studio.detail.result") }}</dt><dd>{{ submission.build?.status || submission.build_result }}</dd></div>
          <div><dt>Git</dt><dd><code>{{ submission.git?.commit || "—" }}</code></dd></div>
          <div><dt>{{ t("studio.detail.predecessor") }}</dt><dd>{{ submission.predecessor?.version_label || "—" }}</dd></div>
        </dl>
        <p v-if="submission.git?.branch">{{ submission.git.branch }}<span v-if="submission.git.remote"> · {{ submission.git.remote }}</span></p>
      </section>

      <div class="submission-detail__actions">
        <a v-if="submission.package_path" class="tb-button tb-button--ghost" :href="artifactUrl" download>{{ t("studio.detail.download") }}</a>
        <button v-if="canReview" class="tb-button tb-button--primary" type="button" @click="reviewOpen = true">{{ t("studio.detail.review") }}</button>
        <button v-if="canPublish" class="tb-button tb-button--primary" type="button" :disabled="working" @click="publish">{{ t("studio.detail.publish") }}</button>
        <button class="tb-button tb-button--ghost" type="button" :disabled="loading" @click="load">↻ {{ t("studio.common.refresh") }}</button>
      </div>
      <p v-if="actionError" class="submission-detail__error" role="alert">{{ actionError }}</p>

      <section class="submission-detail__panel" aria-labelledby="comments-title">
        <h2 id="comments-title">{{ t("studio.detail.comments") }}</h2>
        <p v-if="!submission.comments?.length">{{ t("studio.detail.noComments") }}</p>
        <ol v-else class="submission-detail__timeline">
          <li v-for="comment in submission.comments" :key="comment.id">
            <strong>{{ comment.author }}</strong><time>{{ formatDate(comment.created_at) }}</time>
            <p class="submission-detail__comment">{{ comment.body }}</p>
          </li>
        </ol>
        <form class="submission-detail__comment-form" @submit.prevent="addComment">
          <label class="tb-field">{{ t("studio.detail.addComment") }}<textarea v-model="commentText" class="tb-textarea" maxlength="4000" required></textarea></label>
          <button class="tb-button tb-button--primary" type="submit" :disabled="working || !commentText.trim()">{{ t("studio.detail.comment") }}</button>
        </form>
      </section>

      <section class="submission-detail__panel" aria-labelledby="history-title">
        <h2 id="history-title">{{ t("studio.detail.history") }}</h2>
        <ol class="submission-detail__timeline">
          <li v-for="event in submission.events || []" :key="event.seq">
            <strong>{{ event.event_type }}</strong><time>{{ event.actor_id }} · {{ formatDate(event.created_at) }}</time>
            <code v-if="event.data_json && event.data_json !== '{}'">{{ event.data_json }}</code>
          </li>
        </ol>
      </section>

      <section v-if="submission.effects?.length" class="submission-detail__panel" aria-labelledby="effects-title">
        <h2 id="effects-title">{{ t("studio.detail.effects") }}</h2>
        <ul class="submission-detail__effects">
          <li v-for="effect in submission.effects" :key="effect.id">
            <span>{{ effect.kind }}<template v-if="effect.provider_key"> · {{ effect.provider_key }}</template> · {{ effect.status }}<small v-if="effect.last_error_code">{{ effect.last_error_code }}</small><small v-if="effect.retry_in_flight || effectRetryPolling.has(effect.id)" role="status">{{ t("studio.detail.effectInFlight") }}</small></span>
            <button v-if="canRetryEffects && ['failed', 'delivery_unknown'].includes(effect.status)" class="tb-button tb-button--ghost" type="button" :disabled="working || effect.retry_in_flight || effectRetryPolling.has(effect.id)" @click="retryEffect(effect)">{{ t("studio.common.retry") }}</button>
          </li>
        </ul>
      </section>

      <section class="submission-detail__panel">
        <ArtifactPreview
          v-if="submission.preview?.state === 'ready'"
          subject-kind="submission"
          :subject-id="submission.id"
          :preview-id="submission.preview.id"
        />
        <div v-else class="submission-detail__preview-state" :role="submission.preview?.state === 'failed' ? 'alert' : 'status'" aria-live="polite">
          <strong>{{ t("studio.preview.unavailable") }}</strong>
          <span>{{ t("studio.preview.state", { state: submission.preview?.state || "missing" }) }}</span>
          <code v-if="submission.preview?.error_code">{{ submission.preview.error_code }}</code>
          <p v-if="previewRetry" role="status" aria-live="polite">
            {{ t("studio.preview.progress", { phase: previewRetry.phase || previewRetry.state || "reserved" }) }}
            · {{ previewElapsedSeconds }} s
          </p>
          <button
            v-if="previewPolling"
            class="tb-button tb-button--ghost"
            type="button"
            @click="pausePreviewRetry"
          >{{ t("studio.preview.leave") }}</button>
          <button
            v-if="submission.preview?.recoverable && actions.includes('submission.preview.retry')"
            class="tb-button tb-button--ghost"
            type="button"
            :disabled="previewPolling"
            @click="retryPreview"
          >{{ t("studio.common.retry") }}</button>
        </div>
      </section>

      <section class="submission-detail__panel"><ManifestTable :submission-id="submission.id" /></section>
      <section class="submission-detail__panel"><BuildLogPanel :submission-id="submission.id" :available-phases="submission.log_phases" /></section>

      <ReviewActionDialog
        v-model="reviewOpen"
        :submission-id="submission.id"
        :state="submission.state"
        :row-version="submission.row_version"
        @completed="load"
        @conflict="onConflict"
      />
    </template>
  </section>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from "vue";

import { useI18n } from "../shared/i18n";
import ArtifactPreview from "./ArtifactPreview.vue";
import BuildLogPanel from "./BuildLogPanel.vue";
import ManifestTable from "./ManifestTable.vue";
import ReviewActionDialog from "./ReviewActionDialog.vue";
import SubmissionStatusBadge from "./SubmissionStatusBadge.vue";
import {
  addSubmissionComment,
  getSubmission,
  publishSubmission,
  retrySubmissionEffect,
  retrySubmissionPreview,
  getPreviewOperation,
  submissionArtifactUrl,
} from "./api/studioHttp";

const { t } = useI18n();
const props = defineProps({ submissionId: { type: String, required: true } });
const emit = defineEmits(["back", "project-loaded"]);
const submission = ref(null);
const loading = ref(false);
const error = ref("");
const actionError = ref("");
const working = ref(false);
const commentText = ref("");
const reviewOpen = ref(false);
const previewRetry = ref(null);
const previewPolling = ref(false);
const previewElapsedSeconds = ref(0);
const effectRetryPolling = ref(new Set());
let previewPollGeneration = 0;
let effectPollGeneration = 0;

const actions = computed(() => submission.value?.actions_allowed || []);
const canReview = computed(() => actions.value.includes("submission.review") && ["submitted", "under_review"].includes(submission.value?.state));
const canPublish = computed(() => actions.value.includes("submission.publish"));
const canRetryEffects = computed(() => actions.value.includes("submission.effects.retry"));
const artifactUrl = computed(() => submissionArtifactUrl(props.submissionId));

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    submission.value = await getSubmission(props.submissionId);
    emit("project-loaded", submission.value.project_slug);
    for (const effect of submission.value.effects || []) {
      if (effect.retry_in_flight) void pollEffectUntilSettled(effect.id, effect.retry_after);
    }
    void resumeStoredPreviewRetry();
  } catch (reason) {
    error.value = t("studio.detail.error", { error: reason.message });
  } finally {
    loading.value = false;
  }
}

async function addComment() {
  const text = commentText.value.trim();
  if (!text || working.value) return;
  working.value = true;
  actionError.value = "";
  try {
    const comment = await addSubmissionComment(props.submissionId, text);
    submission.value.comments = [...(submission.value.comments || []), comment];
    commentText.value = "";
  } catch (reason) {
    actionError.value = t("studio.detail.actionError", { error: reason.message });
  } finally {
    working.value = false;
  }
}

async function publish() {
  if (working.value) return;
  working.value = true;
  actionError.value = "";
  try {
    await publishSubmission(props.submissionId, submission.value.row_version);
    await load();
  } catch (reason) {
    actionError.value = reason.status === 409 ? t("studio.review.conflict") : t("studio.detail.actionError", { error: reason.message });
    if (reason.status === 409) await load();
  } finally {
    working.value = false;
  }
}

function setEffectPolling(effectId, active) {
  const next = new Set(effectRetryPolling.value);
  if (active) next.add(effectId);
  else next.delete(effectId);
  effectRetryPolling.value = next;
}

async function pollEffectUntilSettled(effectId, retryAfter = 1) {
  if (effectRetryPolling.value.has(effectId)) return;
  const token = effectPollGeneration;
  let interval = Math.min(Math.max(Number(retryAfter) || 1, 1) * 1000, 5000);
  setEffectPolling(effectId, true);
  try {
    while (token === effectPollGeneration) {
      await delay(interval);
      if (token !== effectPollGeneration) return;
      const refreshed = await getSubmission(props.submissionId);
      submission.value = refreshed;
      emit("project-loaded", refreshed.project_slug);
      const current = (refreshed.effects || []).find((item) => item.id === effectId);
      if (!current || !current.retry_in_flight) return;
      interval = Math.min(Math.max(Number(current.retry_after) || 1, 1) * 1000, 5000);
    }
  } catch (reason) {
    actionError.value = t("studio.detail.actionError", { error: reason.message });
  } finally {
    setEffectPolling(effectId, false);
  }
}

async function retryEffect(effect) {
  if (working.value || effect.retry_in_flight || effectRetryPolling.value.has(effect.id)) return;
  working.value = true;
  actionError.value = "";
  try {
    await retrySubmissionEffect(props.submissionId, effect.id, effect.generation);
    await load();
  } catch (reason) {
    if (reason.status === 409 && reason.payload?.error === "effect_in_flight") {
      effect.retry_in_flight = true;
      effect.retry_after = reason.payload.details?.retry_after || 1;
      void pollEffectUntilSettled(effect.id, effect.retry_after);
    } else {
      actionError.value = reason.status === 409 ? t("studio.review.conflict") : t("studio.detail.actionError", { error: reason.message });
    }
  } finally {
    working.value = false;
  }
}

async function retryPreview() {
  if (previewPolling.value || !submission.value?.preview?.recoverable) return;
  actionError.value = "";
  const stored = readStoredPreviewRetry();
  try {
    if (stored) {
      const operation = await getPreviewOperation(stored.pollUrl || stored.operationId);
      await pollPreviewRetry(operation, stored.key, stored.deadlineAt);
      return;
    }
    const idempotencyKey = globalThis.crypto?.randomUUID?.() || `preview-retry-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const operation = await retrySubmissionPreview(props.submissionId, idempotencyKey);
    await pollPreviewRetry(operation, idempotencyKey);
  } catch (reason) {
    actionError.value = t("studio.detail.actionError", { error: reason.message });
  }
}

function previewRetryStorageKey() {
  return `toolbox.preview-retry.${props.submissionId}`;
}

function readStoredPreviewRetry() {
  try {
    const value = JSON.parse(globalThis.sessionStorage?.getItem(previewRetryStorageKey()) || "null");
    return value && value.operationId && value.key ? value : null;
  } catch {
    return null;
  }
}

function persistPreviewRetry(operation, key, deadlineAt) {
  globalThis.sessionStorage?.setItem(previewRetryStorageKey(), JSON.stringify({
    operationId: operation.operation_id,
    pollUrl: operation.poll_url,
    key,
    deadlineAt,
  }));
}

function clearPreviewRetry() {
  globalThis.sessionStorage?.removeItem(previewRetryStorageKey());
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function pollPreviewRetry(initialOperation, key, storedDeadline = null) {
  const token = ++previewPollGeneration;
  let operation = initialOperation;
  let interval = 500;
  const createdAt = Date.parse(operation.created_at || "");
  const startedAt = Number.isFinite(createdAt) ? createdAt : Date.now();
  const timeoutSeconds = Math.min(Math.max(Number(operation.timeout_seconds) || 900, 1), 900);
  const deadlineAt = storedDeadline || startedAt + timeoutSeconds * 1000;
  previewPolling.value = true;
  previewRetry.value = operation;
  persistPreviewRetry(operation, key, deadlineAt);
  try {
    while (["queued", "running"].includes(operation.state || operation.status)) {
      if (token !== previewPollGeneration) return;
      previewElapsedSeconds.value = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      if (Date.now() >= deadlineAt) throw new Error("preview_poll_timeout");
      if (globalThis.document?.hidden) {
        await delay(1000);
        continue;
      }
      await delay(interval);
      if (token !== previewPollGeneration) return;
      operation = await getPreviewOperation(operation.poll_url || operation.operation_id);
      previewRetry.value = operation;
      persistPreviewRetry(operation, key, deadlineAt);
      interval = Math.min(Math.ceil(interval * 1.5), 5000);
    }
    clearPreviewRetry();
    if ((operation.state || operation.status) !== "ready") {
      throw new Error(operation.error_code || "preview_build_failed");
    }
    await load();
  } finally {
    if (token === previewPollGeneration) {
      previewPolling.value = false;
      if (!["queued", "running"].includes(operation.state || operation.status)) previewRetry.value = null;
    }
  }
}

async function resumeStoredPreviewRetry() {
  const stored = readStoredPreviewRetry();
  if (!stored || previewPolling.value) return;
  try {
    const operation = await getPreviewOperation(stored.pollUrl || stored.operationId);
    await pollPreviewRetry(operation, stored.key, stored.deadlineAt);
  } catch (reason) {
    actionError.value = t("studio.detail.actionError", { error: reason.message });
  }
}

function pausePreviewRetry() {
  previewPollGeneration += 1;
  previewPolling.value = false;
}

function onConflict() {
  actionError.value = t("studio.review.conflict");
  load();
}

function stopPolling() {
  pausePreviewRetry();
  effectPollGeneration += 1;
  effectRetryPolling.value = new Set();
}

watch(() => props.submissionId, () => {
  stopPolling();
  previewRetry.value = null;
  load();
}, { immediate: true });
onUnmounted(stopPolling);
</script>

<style scoped>
.submission-detail { max-width: 1120px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
.submission-detail__head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.submission-detail__head h1 { margin: 3px 0; font-size: 28px; }
.submission-detail__head p { margin: 0; color: var(--toolbox-muted); white-space: pre-wrap; }
.submission-detail__eyebrow { color: var(--toolbox-accent); font-size: 12px; font-weight: 800; text-transform: uppercase; }
.submission-detail__meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 0; }
.submission-detail__meta div, .submission-detail__panel { padding: 14px; border: 1px solid var(--toolbox-border); border-radius: 10px; background: rgb(var(--toolbox-surface-rgb) / 42%); }
.submission-detail__meta--nested { grid-template-columns: repeat(4, 1fr); }
.submission-detail__meta--nested div { padding: 8px; }
dt { color: var(--toolbox-muted); font-size: 10px; font-weight: 800; text-transform: uppercase; } dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.submission-detail__actions { display: flex; gap: 8px; flex-wrap: wrap; }
.submission-detail__error { margin: 0; padding: 10px; border-radius: 8px; color: var(--toolbox-danger); background: var(--toolbox-danger-soft); font-weight: 700; }
.submission-detail__panel { display: grid; gap: 12px; } .submission-detail__panel h2, .submission-detail__panel p { margin: 0; }
.submission-detail__timeline { display: grid; gap: 8px; margin: 0; padding-left: 22px; }
.submission-detail__timeline li { display: grid; gap: 3px; }
time, small { color: var(--toolbox-muted); font-size: 11px; }
.submission-detail__comment { white-space: pre-wrap; overflow-wrap: anywhere; }
.submission-detail__comment-form { display: grid; gap: 8px; }
.submission-detail__comment-form :deep(.tb-textarea) { min-height: 90px; }
.submission-detail__comment-form .tb-button { justify-self: end; }
.submission-detail__effects { list-style: none; display: grid; gap: 8px; margin: 0; padding: 0; }
.submission-detail__effects li { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.submission-detail__effects span { display: grid; }
.submission-detail__panel :deep(.artifact-preview) { padding: 0; }
.submission-detail__preview-state { display: grid; justify-items: start; gap: 8px; color: var(--toolbox-muted); }
code { overflow-wrap: anywhere; font-size: 11px; }
@media (max-width: 760px) { .submission-detail { padding: 14px; } .submission-detail__meta { grid-template-columns: 1fr 1fr; } .submission-detail__head { flex-direction: column; } }
@media (max-width: 430px) { .submission-detail__meta { grid-template-columns: 1fr; } }
</style>
