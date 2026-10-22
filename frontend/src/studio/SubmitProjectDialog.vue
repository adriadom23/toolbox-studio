<template>
  <Modal :model-value="modelValue" :title="t('studio.submit.title')" @update:model-value="onModalUpdate">
    <div class="submit-dialog">
      <p class="submit-dialog__intro">{{ t("studio.submit.intro") }}</p>

      <p v-if="loadError" class="submit-dialog__error" role="alert">{{ loadError }}</p>

      <template v-else>
        <p class="submit-dialog__owners">
          <span class="submit-dialog__label">{{ ownersLine }}</span>
        </p>

        <p v-if="bothDisabled" class="submit-dialog__warn" role="alert">
          {{ t("studio.submit.bothDisabled") }}
        </p>
      </template>

      <!-- Result surface after a submit. -->
      <div v-if="result" class="submit-dialog__result">
        <p class="submit-dialog__done">✓ {{ t("studio.submit.done") }}</p>
        <p class="submit-dialog__package"><code>{{ t("studio.submit.package", { path: result.package_path }) }}</code></p>
        <ul class="submit-dialog__channels">
          <li :class="result.notified.email ? 'is-ok' : 'is-fail'">
            {{ result.notified.email ? t("studio.submit.notifiedEmailOk") : t("studio.submit.notifiedEmailFail") }}
          </li>
          <li :class="result.notified.slack ? 'is-ok' : 'is-fail'">
            {{ result.notified.slack ? t("studio.submit.notifiedSlackOk") : t("studio.submit.notifiedSlackFail") }}
          </li>
        </ul>
        <p v-if="notifyWarning" class="submit-dialog__warn">{{ t("studio.submit.notifyWarning") }}</p>
      </div>

      <p v-if="submitError" class="submit-dialog__error" role="alert">{{ submitError }}</p>
    </div>

    <template #footer>
      <button type="button" class="tb-button tb-button--ghost" @click="close">
        {{ result ? t("common.close") : t("studio.submit.cancel") }}
      </button>
      <button
        v-if="!result"
        type="button"
        class="tb-button tb-button--primary"
        :disabled="submitting || Boolean(loadError)"
        @click="confirmSubmit"
      >
        {{ submitting ? t("studio.submit.sending") : t("studio.submit.confirm") }}
      </button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, ref, watch } from "vue";

import { useI18n } from "../shared/i18n";
import { getNotificationTargets, submitProject } from "./api/studioHttp";

const { t } = useI18n();

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  name: { type: String, required: true },
});
const emit = defineEmits(["update:modelValue", "submitted"]);

const owners = ref([]);
const emailEnabled = ref(false);
const slackEnabled = ref(false);
const loadError = ref("");
const submitting = ref(false);
const submitError = ref("");
const result = ref(null);

const bothDisabled = computed(() => !emailEnabled.value && !slackEnabled.value);

const ownersLine = computed(() => {
  if (!owners.value.length) return t("studio.submit.noOwners");
  return t("studio.submit.willNotify", { owners: owners.value.join(", ") });
});

// A notification "warning" state: the package was built but at least one
// channel that was configured/attempted did not deliver (design §4.6).
const notifyWarning = computed(() => {
  if (!result.value) return false;
  const n = result.value.notified || {};
  return (emailEnabled.value && !n.email) || (slackEnabled.value && !n.slack);
});

async function loadOwners() {
  loadError.value = "";
  submitError.value = "";
  result.value = null;
  try {
    const data = await getNotificationTargets();
    owners.value = Array.isArray(data.owners) ? data.owners : [];
    emailEnabled.value = Boolean(data.email_enabled);
    slackEnabled.value = Boolean(data.slack_enabled);
  } catch (err) {
    loadError.value = t("studio.submit.loadError", { error: err.message });
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) loadOwners();
  },
);

function onModalUpdate(open) {
  emit("update:modelValue", open);
}

function close() {
  emit("update:modelValue", false);
}

async function confirmSubmit() {
  if (submitting.value) return;
  submitting.value = true;
  submitError.value = "";
  try {
    const data = await submitProject(props.name);
    // Defensive shape: the backend always returns notified:{email,slack}.
    data.notified = data.notified || { email: false, slack: false };
    result.value = data;
    emit("submitted", data);
  } catch (err) {
    submitError.value = t("studio.submit.submitError", { error: err.message });
  } finally {
    submitting.value = false;
  }
}
</script>

<style scoped>
.submit-dialog {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.submit-dialog__intro {
  margin: 0;
  color: var(--toolbox-text);
  font-size: 14px;
}

.submit-dialog__label {
  font-weight: 700;
  color: var(--toolbox-text);
}

.submit-dialog__owners {
  margin: 0;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgb(var(--toolbox-surface-rgb) / 45%);
}

.submit-dialog__warn {
  margin: 0;
  padding: 10px 12px;
  border-radius: 10px;
  color: #8a6d0b;
  background: rgb(224 167 46 / 14%);
  border: 1px solid rgb(224 167 46 / 45%);
  font-size: 13px;
  font-weight: 600;
}

.submit-dialog__error {
  margin: 0;
  color: var(--toolbox-danger);
  font-weight: 700;
  font-size: 13px;
}

.submit-dialog__result {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 10px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 55%);
  background: rgb(var(--toolbox-surface-rgb) / 35%);
}

.submit-dialog__done {
  margin: 0;
  color: #1f6f49;
  font-weight: 700;
}

.submit-dialog__package code {
  font-size: 11.5px;
  word-break: break-all;
  color: var(--toolbox-muted);
}

.submit-dialog__channels {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.submit-dialog__channels li {
  font-size: 12.5px;
  font-weight: 700;
}

.submit-dialog__channels .is-ok {
  color: #1f6f49;
}

.submit-dialog__channels .is-fail {
  color: var(--toolbox-muted);
}
</style>
