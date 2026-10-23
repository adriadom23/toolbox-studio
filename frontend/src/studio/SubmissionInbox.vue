<template>
  <section class="submission-inbox tool-surface">
    <header class="submission-inbox__head">
      <div><h1>{{ t("studio.inbox.title") }}</h1><p>{{ t("studio.inbox.subtitle") }}</p></div>
      <button class="tb-button tb-button--ghost" type="button" :disabled="loading" @click="load(false)">↻ {{ t("studio.common.refresh") }}</button>
    </header>
    <form class="submission-inbox__filters" @submit.prevent="load(false)">
      <label class="tb-field">{{ t("studio.inbox.project") }}<input v-model.trim="projectFilter" class="tb-input" /></label>
      <label class="tb-field">{{ t("studio.inbox.status") }}<select v-model="statusFilter" class="tb-select"><option value="">{{ t("studio.inbox.all") }}</option><option v-for="status in statuses" :key="status" :value="status">{{ t(`studio.status.${status}`) }}</option></select></label>
      <label class="tb-field">{{ t("studio.inbox.author") }}<input v-model.trim="authorFilter" class="tb-input" /></label>
      <label class="tb-field">{{ t("studio.inbox.createdFrom") }}<input v-model="createdFromFilter" class="tb-input" type="datetime-local" /></label>
      <label class="tb-field">{{ t("studio.inbox.createdTo") }}<input v-model="createdToFilter" class="tb-input" type="datetime-local" /></label>
      <button class="tb-button tb-button--primary" type="submit" :disabled="loading">{{ t("studio.inbox.filter") }}</button>
    </form>
    <p v-if="loading && !items.length" role="status">{{ t("common.processing") }}</p>
    <p v-if="error" class="submission-inbox__error" role="alert">{{ error }} <button type="button" @click="load(false)">{{ t("studio.common.retry") }}</button></p>
    <p v-if="!loading && !error && !items.length" class="submission-inbox__empty">{{ t("studio.inbox.empty") }}</p>
    <ul v-if="items.length" class="submission-inbox__list">
      <li v-for="item in items" :key="item.id">
        <button type="button" @click="emit('open', item.id)">
          <span>
            <strong>{{ item.title }}</strong>
            <small>{{ item.project_slug }} · {{ item.version_label }} · #{{ item.short_id }}</small>
            <small>{{ item.author_display || item.author_id }} · {{ formatDate(item.created_at) }} · build: {{ item.build_result }}</small>
          </span>
          <SubmissionStatusBadge :status="item.state" />
        </button>
      </li>
    </ul>
    <button v-if="nextCursor" class="tb-button tb-button--ghost" type="button" :disabled="loading" @click="load(true)">{{ t("studio.common.loadMore") }}</button>
  </section>
</template>

<script setup>
import { onMounted, ref } from "vue";

import { useI18n } from "../shared/i18n";
import { listSubmissions } from "./api/studioHttp";
import SubmissionStatusBadge from "./SubmissionStatusBadge.vue";

const { t } = useI18n();
const emit = defineEmits(["open"]);
const statuses = ["building", "submitted", "under_review", "changes_requested", "rejected", "approved", "published", "build_failed"];
const items = ref([]);
const nextCursor = ref(null);
const projectFilter = ref("");
const statusFilter = ref("");
const authorFilter = ref("");
const createdFromFilter = ref("");
const createdToFilter = ref("");
const loading = ref(false);
const error = ref("");

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function load(append) {
  loading.value = true;
  error.value = "";
  try {
    const data = await listSubmissions({
      cursor: append ? nextCursor.value : null,
      projectSlug: projectFilter.value,
      status: statusFilter.value,
      author: authorFilter.value,
      createdFrom: createdFromFilter.value ? new Date(createdFromFilter.value).toISOString() : "",
      createdTo: createdToFilter.value ? new Date(createdToFilter.value).toISOString() : "",
    });
    items.value = append ? [...items.value, ...(data.items || [])] : (data.items || []);
    nextCursor.value = data.next_cursor || null;
  } catch (reason) {
    error.value = t("studio.inbox.error", { error: reason.message });
  } finally {
    loading.value = false;
  }
}

onMounted(() => load(false));
</script>

<style scoped>
.submission-inbox { max-width: 1100px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
.submission-inbox__head { display: flex; justify-content: space-between; gap: 14px; }
h1, p { margin: 0; } h1 { font-size: 26px; } .submission-inbox__head p { margin-top: 4px; color: var(--toolbox-muted); }
.submission-inbox__filters { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)) auto; align-items: end; gap: 10px; }
.submission-inbox__error { color: var(--toolbox-danger); font-weight: 700; }
.submission-inbox__empty { padding: 50px; color: var(--toolbox-muted); text-align: center; }
.submission-inbox__list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.submission-inbox__list button { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px; border: 1px solid var(--toolbox-border); border-radius: 10px; color: var(--toolbox-text); background: rgb(var(--toolbox-surface-rgb) / 55%); font: inherit; text-align: left; cursor: pointer; }
.submission-inbox__list button:hover, .submission-inbox__list button:focus-visible { border-color: var(--toolbox-accent); }
.submission-inbox__list span:first-child { display: grid; gap: 4px; } small { color: var(--toolbox-muted); }
@media (max-width: 650px) { .submission-inbox { padding: 14px; } .submission-inbox__filters { grid-template-columns: 1fr; } .submission-inbox__head { flex-direction: column; } }
</style>
