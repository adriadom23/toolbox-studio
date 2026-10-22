<template>
  <div class="project-list tool-surface">
    <header class="project-list__head">
      <div>
        <h1 class="project-list__title">{{ t("studio.list.title") }}</h1>
        <p class="project-list__subtitle">{{ t("studio.list.subtitle") }}</p>
      </div>
      <button type="button" class="tb-button tb-button--primary" @click="showNewModal = true">
        <span aria-hidden="true">＋</span> {{ t("studio.list.newProject") }}
      </button>
    </header>

    <p v-if="loadError" class="project-list__error" role="alert">{{ loadError }}</p>

    <p v-else-if="loading" class="project-list__empty">{{ t("common.processing") }}</p>

    <p v-else-if="!projects.length" class="project-list__empty">{{ t("studio.list.empty") }}</p>

    <ul v-else class="project-list__grid">
      <li v-for="project in projects" :key="project.name" class="project-card">
        <div class="project-card__head">
          <h2 class="project-card__name">{{ project.name }}</h2>
          <span
            class="project-card__badge"
            :class="`project-card__badge--${project.status || 'draft'}`"
          >
            {{ project.status === "submitted" ? t("studio.list.statusSubmitted") : t("studio.list.statusDraft") }}
          </span>
        </div>
        <dl class="project-card__meta">
          <div>
            <dt>{{ t("studio.list.created") }}</dt>
            <dd>{{ formatStamp(project.created_at) }}</dd>
          </div>
          <div>
            <dt>{{ t("studio.list.modified") }}</dt>
            <dd>{{ formatStamp(project.modified_at) }}</dd>
          </div>
        </dl>
        <div class="project-card__actions">
          <button type="button" class="tb-button tb-button--primary" @click="emit('open', project.name)">
            {{ t("studio.list.open") }}
          </button>
          <button type="button" class="tb-button tb-button--ghost" @click="confirmDelete(project)">
            {{ t("studio.list.delete") }}
          </button>
        </div>
      </li>
    </ul>

    <NewProjectModal v-model="showNewModal" @created="onCreated" />
  </div>
</template>

<script setup>
import { onMounted, ref } from "vue";

import { useI18n } from "../shared/i18n";
import { showToolboxError, showToolboxWarning } from "../shared/notifications/swal";
import { listProjects, deleteProject } from "./api/studioHttp";
import NewProjectModal from "./NewProjectModal.vue";

const { t } = useI18n();
const emit = defineEmits(["open"]);

const projects = ref([]);
const loading = ref(true);
const loadError = ref("");
const showNewModal = ref(false);

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const result = await listProjects();
    projects.value = Array.isArray(result) ? result : [];
  } catch (err) {
    loadError.value = t("studio.list.loadError", { error: err.message });
  } finally {
    loading.value = false;
  }
}

function formatStamp(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString();
}

// Explicit, fail-closed delete confirmation (UI half of the two-sided
// delete-confirmation requirement; the backend requires {confirm:true} too).
// Enter immediately after opening chooses Cancel (focusCancel in swal.js) so a
// stray keypress never deletes a project.
async function confirmDelete(project) {
  const { isConfirmed } = await showToolboxWarning({
    title: t("studio.list.deleteTitle", { name: project.name }),
    text: t("studio.list.deleteText"),
    showCancelButton: true,
    confirmButtonText: t("studio.list.deleteConfirm"),
    cancelButtonText: t("studio.list.deleteCancel"),
  });
  if (!isConfirmed) return;
  try {
    await deleteProject(project.name);
    projects.value = projects.value.filter((p) => p.name !== project.name);
  } catch (err) {
    showToolboxError({ title: t("studio.list.delete"), text: t("studio.list.deleteError", { error: err.message }) });
  }
}

function onCreated(name) {
  showNewModal.value = false;
  emit("open", name);
}

onMounted(load);
</script>

<style scoped>
.project-list {
  max-width: 1320px;
  margin: 0 auto;
  padding: 24px;
}

.project-list__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 22px;
}

.project-list__title {
  margin: 0;
  font-family: var(--tb-font-heading);
  font-size: 26px;
  color: var(--toolbox-text);
}

.project-list__subtitle {
  margin: 4px 0 0;
  color: var(--toolbox-muted);
  font-size: 14px;
}

.project-list__error {
  color: var(--toolbox-danger);
  font-weight: 700;
}

.project-list__empty {
  color: var(--toolbox-muted);
  padding: 40px 0;
  text-align: center;
}

.project-list__grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}

.project-card {
  border: 1px solid rgb(var(--toolbox-border-rgb) / 55%);
  border-radius: 14px;
  padding: 16px;
  background: rgb(var(--toolbox-surface-rgb) / 40%);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.project-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.project-card__name {
  margin: 0;
  font-size: 17px;
  color: var(--toolbox-text);
  word-break: break-word;
}

.project-card__badge {
  flex: none;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.project-card__badge--draft {
  color: var(--toolbox-muted);
  border: 1px solid rgb(var(--toolbox-border-rgb) / 60%);
}

.project-card__badge--submitted {
  color: #1f6f49;
  border: 1px solid rgb(46 166 90 / 45%);
  background: rgb(46 166 90 / 12%);
}

.project-card__meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin: 0;
}

.project-card__meta dt {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--toolbox-muted);
}

.project-card__meta dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  color: var(--toolbox-text);
}

.project-card__actions {
  display: flex;
  gap: 8px;
  margin-top: auto;
}
</style>
