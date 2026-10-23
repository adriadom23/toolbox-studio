<template>
  <Modal :model-value="modelValue" :title="t('studio.review.title')" @update:model-value="emit('update:modelValue', $event)">
    <form class="review-dialog" @submit.prevent="submit">
      <label class="tb-field">
        {{ t("studio.review.action") }}
        <select v-model="action" class="tb-select" :disabled="sending">
          <option v-for="item in actions" :key="item" :value="item">{{ t(`studio.review.${item}`) }}</option>
        </select>
      </label>
      <label v-if="showsComment" class="tb-field">
        {{ needsReason ? t("studio.review.reason") : t("studio.review.optionalComment") }}
        <textarea v-model.trim="reason" class="tb-textarea" maxlength="4000" :required="needsReason"></textarea>
      </label>
      <p v-if="error" class="review-dialog__error" role="alert">{{ error }}</p>
    </form>
    <template #footer>
      <button class="tb-button tb-button--ghost" type="button" :disabled="sending" @click="emit('update:modelValue', false)">{{ t("common.close") }}</button>
      <button class="tb-button tb-button--primary" type="button" :disabled="sending || (needsReason && !reason)" @click="submit">{{ sending ? t("common.processing") : t("studio.review.confirm") }}</button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, ref, watch } from "vue";

import Modal from "../shared/components/Modal.vue";
import { useI18n } from "../shared/i18n";
import { reviewSubmission } from "./api/studioHttp";

const { t } = useI18n();
const props = defineProps({
  modelValue: { type: Boolean, required: true },
  submissionId: { type: String, required: true },
  state: { type: String, required: true },
  rowVersion: { type: Number, required: true },
});
const emit = defineEmits(["update:modelValue", "completed", "conflict"]);
const action = ref("start");
const reason = ref("");
const error = ref("");
const sending = ref(false);
const actions = computed(() => props.state === "submitted" ? ["start"] : props.state === "under_review" ? ["request_changes", "reject", "approve"] : []);
const needsReason = computed(() => ["request_changes", "reject"].includes(action.value));
const showsComment = computed(() => needsReason.value || action.value === "approve");

watch([() => props.modelValue, actions], ([open, values]) => {
  if (!open) return;
  action.value = values[0] || "start";
  reason.value = "";
  error.value = "";
});

async function submit() {
  if (sending.value || !actions.value.includes(action.value)) return;
  sending.value = true;
  error.value = "";
  try {
    const data = await reviewSubmission(props.submissionId, {
      action: action.value,
      expected_row_version: props.rowVersion,
      reason: reason.value || null,
      publication_target: "none",
    });
    emit("completed", data);
    emit("update:modelValue", false);
  } catch (reasonError) {
    if (reasonError.status === 409) {
      error.value = t("studio.review.conflict");
      emit("conflict");
    } else {
      error.value = t("studio.review.error", { error: reasonError.message });
    }
  } finally {
    sending.value = false;
  }
}
</script>

<style scoped>
.review-dialog { display: grid; gap: 14px; }
.review-dialog :deep(.tb-textarea) { min-height: 120px; }
.review-dialog__error { margin: 0; color: var(--toolbox-danger); font-weight: 700; }
</style>
