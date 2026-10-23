<template>
  <section class="log-panel" aria-labelledby="log-title">
    <div class="log-panel__head">
      <h2 id="log-title">{{ t("studio.detail.logs") }}</h2>
      <div role="tablist" :aria-label="t('studio.detail.logs')">
        <button v-for="item in phases" :key="item" type="button" role="tab" class="log-panel__tab" :aria-selected="phase === item" @click="selectPhase(item)">{{ item }}</button>
      </div>
    </div>
    <p v-if="error" class="log-panel__error" role="alert">{{ error }} <button type="button" @click="load(false)">{{ t("studio.common.retry") }}</button></p>
    <p v-if="truncated" class="log-panel__warning" role="status">{{ t("studio.detail.logsTruncated") }}</p>
    <pre v-if="lines.length" tabindex="0">{{ lines.join("\n") }}</pre>
    <p v-else-if="loading" role="status">{{ t("common.processing") }}</p>
    <p v-else>{{ t("studio.detail.noLogs") }}</p>
    <button v-if="nextCursor" class="tb-button tb-button--ghost" type="button" :disabled="loading" @click="load(true)">{{ t("studio.common.loadMore") }}</button>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";

import { useI18n } from "../shared/i18n";
import { getSubmissionLogs } from "./api/studioHttp";

const { t } = useI18n();
const props = defineProps({
  submissionId: { type: String, required: true },
  availablePhases: { type: Array, default: () => ["frontend", "backend"] },
});
const allowedPhases = new Set(["frontend", "backend", "remote"]);
const phases = computed(() => {
  const values = props.availablePhases.filter((item) => allowedPhases.has(item));
  return values.length ? values : ["frontend"];
});
const phase = ref(phases.value[0]);
const lines = ref([]);
const nextCursor = ref(null);
const truncated = ref(false);
const loading = ref(false);
const error = ref("");

async function load(append = false) {
  loading.value = true;
  error.value = "";
  try {
    const data = await getSubmissionLogs(props.submissionId, {
      phase: phase.value,
      cursor: append ? nextCursor.value : null,
    });
    lines.value = append ? [...lines.value, ...(data.lines || [])] : (data.lines || []);
    nextCursor.value = data.next_cursor || null;
    truncated.value = Boolean(data.truncated);
  } catch (reason) {
    error.value = t("studio.detail.partialError", { error: reason.message });
  } finally {
    loading.value = false;
  }
}

function selectPhase(value) {
  phase.value = value;
  lines.value = [];
  nextCursor.value = null;
  truncated.value = false;
  load(false);
}

onMounted(() => load(false));
watch(() => props.submissionId, () => {
  phase.value = phases.value[0];
  truncated.value = false;
  load(false);
});
watch(phases, (values) => {
  if (!values.includes(phase.value)) {
    phase.value = values[0];
    truncated.value = false;
    load(false);
  }
});
</script>

<style scoped>
.log-panel { display: grid; gap: 10px; }
.log-panel__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
h2, p { margin: 0; }
.log-panel__tab { padding: 6px 10px; border: 1px solid var(--toolbox-border); background: var(--toolbox-panel); color: var(--toolbox-text); cursor: pointer; }
.log-panel__tab[aria-selected="true"] { color: white; background: var(--toolbox-accent); }
pre { max-height: 340px; margin: 0; padding: 12px; overflow: auto; border-radius: 8px; color: #d8e4ee; background: #111923; font: 12px/1.5 var(--tb-font-mono, monospace); white-space: pre-wrap; }
.log-panel__error { color: var(--toolbox-danger); }
.log-panel__warning { color: var(--toolbox-warning); font-weight: 700; }
</style>
