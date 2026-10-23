<template>
  <Modal
    :model-value="modelValue"
    :title="t('studio.submit.title')"
    :aria-description="t('studio.submit.intro')"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <form class="submit-dialog" @submit.prevent="confirmSubmit">
      <p class="submit-dialog__intro">{{ t("studio.submit.intro") }}</p>
      <p v-if="dirty" class="submit-dialog__warn" role="alert">
        {{ t("studio.shell.saveBeforeSubmit") }}
      </p>
      <p v-if="contextLoading" class="submit-dialog__muted" role="status">
        {{ t("studio.submit.loadingContext") }}
      </p>
      <p v-if="loadError" class="submit-dialog__error" role="alert">{{ loadError }}</p>

      <dl v-if="context" class="submit-dialog__context">
        <div><dt>{{ t("studio.submit.project") }}</dt><dd>{{ context.project_slug }}</dd></div>
        <div><dt>{{ t("studio.submit.version") }}</dt><dd>v{{ context.next_version }}</dd></div>
        <div><dt>{{ t("studio.submit.author") }}</dt><dd>{{ context.actor?.display_name || context.actor?.id }}</dd></div>
        <div><dt>{{ t("studio.submit.buildProvider") }}</dt><dd>{{ context.providers?.build }}</dd></div>
        <div><dt>{{ t("studio.submit.artifactProvider") }}</dt><dd>{{ context.providers?.artifact }}</dd></div>
        <div><dt>{{ t("studio.submit.logProvider") }}</dt><dd>{{ context.providers?.logs }}</dd></div>
        <div><dt>{{ t("studio.submit.notificationProviders") }}</dt><dd>{{ (context.providers?.notifications || []).join(", ") || "—" }}</dd></div>
        <div><dt>{{ t("studio.submit.publicationProvider") }}</dt><dd>{{ context.providers?.publication }}</dd></div>
        <div><dt>Git</dt><dd>{{ gitSummary }}</dd></div>
      </dl>
      <ul v-if="context?.warnings?.length" class="submit-dialog__warnings" aria-label="Advertencias del envío">
        <li v-for="warning in context.warnings" :key="warning">{{ warning }}</li>
      </ul>

      <label v-if="context?.predecessors?.length" class="tb-field">
        {{ t("studio.submit.predecessor") }}
        <select v-model="predecessorId" class="tb-select" :disabled="operationActive">
          <option value="">{{ t("studio.submit.noPredecessor") }}</option>
          <option v-for="item in context.predecessors" :key="item.id" :value="item.id">
            {{ item.version_label }} · {{ item.state }}
          </option>
        </select>
      </label>
      <label class="tb-field">
        {{ t("studio.submit.titleLabel") }}
        <input ref="titleInput" v-model.trim="title" class="tb-input" maxlength="200" :disabled="operationActive" required />
      </label>
      <label class="tb-field">
        {{ t("studio.submit.descriptionLabel") }}
        <textarea v-model.trim="description" class="tb-textarea" maxlength="4000" :disabled="operationActive" required></textarea>
      </label>

      <ol v-if="operationPhases.length" class="submit-dialog__phases" aria-live="polite">
        <li
          v-for="phase in operationPhases"
          :key="phase.phase"
          :class="{ 'is-done': phase.status === 'succeeded', 'is-current': phase.status === 'running', 'is-failed': ['failed', 'delivery_unknown'].includes(phase.status) }"
        >
          {{ phaseLabel(phase.phase) }} · {{ phase.status }}
          <code v-if="phase.error_code">{{ phase.error_code }}</code>
        </li>
      </ol>

      <div
        v-if="result"
        ref="resultRegion"
        class="submit-dialog__result"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        tabindex="-1"
      >
        <strong>✓ {{ t("studio.submit.done") }}</strong>
        <span>{{ t("studio.submit.versionReady", { version: result.version }) }}</span>
        <span>{{ t("studio.submit.identifier") }}: <code>{{ result.submission_id }}</code></span>
        <span>{{ t("studio.submit.status") }}: {{ t(`studio.status.${result.status}`) }}</span>
        <code v-if="result.artifact_sha256">SHA-256 {{ result.artifact_sha256 }}</code>
        <ul
          v-if="result.notifications?.length"
          class="submit-dialog__notifications"
          :aria-label="t('studio.submit.notificationProviders')"
        >
          <li
            v-for="notification in result.notifications"
            :key="notification.provider_key"
            :class="{ 'is-failed': ['failed', 'delivery_unknown'].includes(notification.status) }"
          >
            {{ notification.provider_key }} · {{ notification.status }}
            <code v-if="notification.error_code">{{ notification.error_code }}</code>
          </li>
        </ul>
      </div>
      <p v-if="submitError" class="submit-dialog__error" role="alert">{{ submitError }}</p>
    </form>

    <template #footer>
      <button type="button" class="tb-button tb-button--ghost" @click="emit('update:modelValue', false)">
        {{ operationActive ? t("studio.submit.closeContinues") : t("common.close") }}
      </button>
      <button
        v-if="result"
        type="button"
        class="tb-button tb-button--ghost"
        @click="startNewAttempt"
      >
        {{ t("studio.submit.newSubmission") }}
      </button>
      <button
        v-if="result"
        type="button"
        class="tb-button tb-button--primary"
        @click="openResult"
      >
        {{ t("studio.submit.openSubmission") }}
      </button>
      <button
        v-else
        type="button"
        class="tb-button tb-button--primary"
        :disabled="operationActive || contextLoading || Boolean(loadError) || dirty || !description || !title"
        @click="confirmSubmit"
      >
        {{ operationActive ? t("studio.submit.sending") : t("studio.submit.confirm") }}
      </button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import Modal from "../shared/components/Modal.vue";
import { useI18n } from "../shared/i18n";
import {
  createSubmission,
  getSubmissionContext,
  getSubmissionOperation,
} from "./api/studioHttp";

const { t } = useI18n();
const props = defineProps({
  modelValue: { type: Boolean, required: true },
  name: { type: String, default: "" },
  dirty: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "open-submission"]);

const context = ref(null);
const contextLoading = ref(false);
const loadError = ref("");
const title = ref("");
const titleInput = ref(null);
const description = ref("");
const predecessorId = ref("");
const operationActive = ref(false);
const operationId = ref("");
const operationPhases = ref([]);
const idempotencyKey = ref("");
const submitError = ref("");
const result = ref(null);
const resultRegion = ref(null);
let pollTimer = null;
const gitSummary = computed(() => {
  const git = context.value?.git;
  if (!git) return "—";
  return [git.branch, git.commit, git.remote].filter(Boolean).join(" · ") || "—";
});

function newKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `studio-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function resetAttempt() {
  context.value = null;
  loadError.value = "";
  title.value = t("studio.submit.defaultTitle", { project: props.name });
  description.value = "";
  predecessorId.value = "";
  operationId.value = "";
  operationPhases.value = [];
  operationActive.value = false;
  result.value = null;
  submitError.value = "";
  idempotencyKey.value = newKey();
}

async function loadContext() {
  if (!props.name) return;
  contextLoading.value = true;
  loadError.value = "";
  try {
    context.value = await getSubmissionContext(props.name);
  } catch (error) {
    loadError.value = t("studio.submit.loadError", { error: error.message });
  } finally {
    contextLoading.value = false;
  }
}

async function startNewAttempt() {
  window.clearTimeout(pollTimer);
  resetAttempt();
  await loadContext();
  await nextTick();
  titleInput.value?.focus({ preventScroll: true });
}

function phaseLabel(phase) {
  const labels = {
    validation: "studio.submit.phaseValidation",
    snapshot: "studio.submit.phaseSnapshot",
    build: "studio.submit.phaseBuild",
    package: "studio.submit.phasePackage",
    register: "studio.submit.phaseRegister",
    notify: "studio.submit.phaseNotify",
  };
  return t(labels[phase] || phase);
}

function operationNeedsPolling(data) {
  if (data.status === "building") return true;
  if (data.status !== "submitted") return false;
  const notify = (data.phases || []).find((phase) => phase.phase === "notify");
  return Boolean(notify && !["succeeded", "failed", "delivery_unknown"].includes(notify.status));
}

function finishOperation(data) {
  operationActive.value = false;
  operationPhases.value = data.phases || operationPhases.value;
  if (data.status === "submitted") {
    result.value = data;
    nextTick(() => resultRegion.value?.focus({ preventScroll: true }));
  } else {
    submitError.value = t("studio.submit.buildFailed", { error: data.error || data.status });
  }
}

async function poll() {
  if (!operationActive.value) return;
  try {
    const data = await getSubmissionOperation(operationId.value);
    operationPhases.value = data.phases || operationPhases.value;
    if (operationNeedsPolling(data)) {
      pollTimer = window.setTimeout(poll, 750);
      return;
    }
    finishOperation(data);
  } catch (error) {
    submitError.value = t("studio.submit.pollError", { error: error.message });
    pollTimer = window.setTimeout(poll, 1500);
  }
}

async function confirmSubmit() {
  if (operationActive.value || !props.name || !title.value || !description.value) return;
  operationActive.value = true;
  submitError.value = "";
  try {
    const data = await createSubmission({
      projectSlug: props.name,
      title: title.value,
      description: description.value,
      predecessorId: predecessorId.value,
      idempotencyKey: idempotencyKey.value,
    });
    operationId.value = data.operation_id;
    operationPhases.value = data.phases || [];
    if (operationNeedsPolling(data)) pollTimer = window.setTimeout(poll, 300);
    else finishOperation(data);
  } catch (error) {
    operationActive.value = false;
    submitError.value = error.status === 409
      ? t("studio.submit.conflict")
      : t("studio.submit.submitError", { error: error.message });
  }
}

function openResult() {
  emit("update:modelValue", false);
  emit("open-submission", result.value);
}

watch(
  () => props.modelValue,
  (open) => {
    if (open && !operationActive.value && !result.value) {
      resetAttempt();
      loadContext();
    }
  },
);

watch(
  () => props.name,
  () => {
    if (!operationActive.value) resetAttempt();
  },
);

onBeforeUnmount(() => window.clearTimeout(pollTimer));
</script>

<style scoped>
.submit-dialog { display: grid; gap: 14px; }
.submit-dialog__intro, .submit-dialog__muted { margin: 0; color: var(--toolbox-muted); }
.submit-dialog__warn, .submit-dialog__error { margin: 0; padding: 10px; border-radius: 8px; font-weight: 700; }
.submit-dialog__warn { color: var(--toolbox-warning); background: var(--toolbox-warning-soft); }
.submit-dialog__warnings { margin: 0; padding: 10px 10px 10px 28px; border-radius: 8px; color: var(--toolbox-warning); background: var(--toolbox-warning-soft); }
.submit-dialog__error { color: var(--toolbox-danger); background: var(--toolbox-danger-soft); }
.submit-dialog__context { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 0; }
.submit-dialog__context div { padding: 10px; border-radius: 8px; background: rgb(var(--toolbox-surface-rgb) / 55%); }
.submit-dialog__context dt { color: var(--toolbox-muted); font-size: 11px; text-transform: uppercase; }
.submit-dialog__context dd { margin: 3px 0 0; color: var(--toolbox-text); font-weight: 700; word-break: break-word; }
.submit-dialog :deep(.tb-textarea) { min-height: 110px; }
.submit-dialog__phases { margin: 0; padding-left: 24px; color: var(--toolbox-muted); }
.submit-dialog__phases li { padding: 3px; }
.submit-dialog__phases .is-current { color: var(--toolbox-accent); font-weight: 800; }
.submit-dialog__phases .is-done { color: var(--toolbox-success); }
.submit-dialog__phases .is-failed { color: var(--toolbox-danger); font-weight: 800; }
.submit-dialog__phases code { display: block; overflow-wrap: anywhere; }
.submit-dialog__result { display: grid; gap: 6px; padding: 12px; border: 1px solid var(--toolbox-success); border-radius: 8px; color: var(--toolbox-success); }
.submit-dialog__result code { color: var(--toolbox-muted); font-size: 11px; word-break: break-all; }
.submit-dialog__notifications { margin: 4px 0 0; padding-left: 20px; color: var(--toolbox-text); }
.submit-dialog__notifications .is-failed { color: var(--toolbox-danger); font-weight: 700; }
.submit-dialog__notifications code { display: block; }
@media (max-width: 600px) { .submit-dialog__context { grid-template-columns: 1fr; } }
</style>
