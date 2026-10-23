<template>
  <section class="manifest-panel" aria-labelledby="manifest-title">
    <div class="manifest-panel__head">
      <h2 id="manifest-title">{{ t("studio.detail.manifest") }}</h2>
      <button v-if="error" class="tb-button tb-button--ghost" type="button" @click="load">{{ t("studio.common.retry") }}</button>
    </div>
    <p v-if="loading && !manifest" role="status">{{ t("common.processing") }}</p>
    <p v-if="error" class="manifest-panel__error" role="alert">{{ error }}</p>
    <div v-if="manifest" class="tb-table-wrap">
      <table class="tb-table">
        <thead><tr><th>{{ t("studio.detail.path") }}</th><th>{{ t("studio.detail.kind") }}</th><th>{{ t("studio.detail.size") }}</th><th>SHA-256</th></tr></thead>
        <tbody>
          <tr v-for="file in manifest.files || []" :key="file.path">
            <td><code>{{ file.path }}</code></td><td>{{ file.kind }}</td><td>{{ formatSize(file.size) }}</td><td><code>{{ file.sha256 }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>
    <button v-if="nextCursor" class="tb-button tb-button--ghost" type="button" :disabled="loading" @click="load(true)">{{ t("studio.common.loadMore") }}</button>
  </section>
</template>

<script setup>
import { onMounted, ref, watch } from "vue";

import { useI18n } from "../shared/i18n";
import { getSubmissionManifest } from "./api/studioHttp";

const { t } = useI18n();
const props = defineProps({ submissionId: { type: String, required: true } });
const manifest = ref(null);
const nextCursor = ref(null);
const loading = ref(false);
const error = ref("");

function formatSize(size) {
  return new Intl.NumberFormat().format(Number(size || 0));
}

async function load(append = false) {
  loading.value = true;
  error.value = "";
  try {
    const data = await getSubmissionManifest(props.submissionId, {
      cursor: append ? nextCursor.value : null,
    });
    manifest.value = append
      ? { ...data, files: [...(manifest.value?.files || []), ...(data.files || [])] }
      : data;
    nextCursor.value = data.next_cursor || null;
  } catch (reason) {
    error.value = t("studio.detail.partialError", { error: reason.message });
  } finally {
    loading.value = false;
  }
}

function resetAndLoad() {
  manifest.value = null;
  nextCursor.value = null;
  load(false);
}

onMounted(resetAndLoad);
watch(() => props.submissionId, resetAndLoad);
</script>

<style scoped>
.manifest-panel { display: grid; gap: 10px; }
.manifest-panel__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
h2 { margin: 0; font-size: 18px; }
p { margin: 0; color: var(--toolbox-muted); }
.manifest-panel__error { color: var(--toolbox-danger); }
code { font-size: 11px; overflow-wrap: anywhere; }
</style>
