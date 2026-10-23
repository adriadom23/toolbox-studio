<template>
  <div class="studio-app">
    <StudioShell
      :active-project="activeProject"
      :dirty="dirty"
      :current-path="route.path"
      :capabilities="sessionCapabilities"
      :project-submit-state="projectSubmitState"
      @navigate="navigate"
      @open-submission="openSubmission"
    />
    <main class="studio-app__view">
      <section v-if="!sessionReady" class="studio-app__bootstrap" role="status">
        <p>{{ sessionError || t("common.processing") }}</p>
        <button v-if="sessionError" class="tb-button tb-button--primary" type="button" @click="bootstrapSession">
          {{ t("studio.common.retry") }}
        </button>
      </section>
      <ProjectList
        v-else-if="route.name === 'projects'"
        :can-create="sessionCapabilities.includes('project.create')"
        @open="openEditor"
      />
      <ProjectEditor
        v-else-if="route.name === 'editor'"
        :name="route.projectSlug"
        @back="navigate('/')"
        @dirty-change="dirty = $event"
        @preview="navigate(`/projects/${encodeURIComponent(route.projectSlug)}/preview`)"
      />
      <ArtifactPreview
        v-else-if="route.name === 'preview'"
        subject-kind="workspace"
        :subject-id="route.projectSlug"
      />
      <SubmissionInbox
        v-else-if="route.name === 'submissions'"
        @open="openSubmission"
      />
      <SubmissionDetail
        v-else-if="route.name === 'submission'"
        :submission-id="route.submissionId"
        @back="navigate('/submissions')"
        @project-loaded="handleProjectLoaded"
      />
      <section v-else class="studio-app__not-found" role="alert">
        <h1>{{ t("studio.routes.notFound") }}</h1>
        <button class="tb-button tb-button--primary" type="button" @click="navigate('/')">
          {{ t("studio.routes.backHome") }}
        </button>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { useI18n } from "./shared/i18n";
import ArtifactPreview from "./studio/ArtifactPreview.vue";
import ProjectEditor from "./studio/ProjectEditor.vue";
import ProjectList from "./studio/ProjectList.vue";
import StudioShell from "./studio/StudioShell.vue";
import SubmissionDetail from "./studio/SubmissionDetail.vue";
import SubmissionInbox from "./studio/SubmissionInbox.vue";
import { getSession, getSubmissionContext } from "./studio/api/studioHttp";

const { t } = useI18n();
const locationPath = ref(window.location.pathname);
const dirty = ref(false);
const detailProject = ref("");
const sessionCapabilities = ref([]);
const sessionReady = ref(false);
const sessionError = ref("");
const projectSubmitState = ref("none");
let projectAccessGeneration = 0;

function decodeSegment(value) {
  try {
    return decodeURIComponent(value);
  } catch (_error) {
    return "";
  }
}

function parseRoute(path) {
  if (path === "/" || path === "") return { name: "projects", path: "/" };
  if (path === "/submissions" || path === "/submissions/") {
    return { name: "submissions", path: "/submissions" };
  }
  let match = /^\/projects\/([^/]+)\/preview\/?$/.exec(path);
  if (match) return { name: "preview", projectSlug: decodeSegment(match[1]), path };
  match = /^\/projects\/([^/]+)\/?$/.exec(path);
  if (match) return { name: "editor", projectSlug: decodeSegment(match[1]), path };
  match = /^\/submissions\/(sub_[0-9a-f]{32})\/?$/.exec(path);
  if (match) return { name: "submission", submissionId: match[1], path };
  return { name: "not-found", path };
}

const route = computed(() => parseRoute(locationPath.value));
const activeProject = computed({
  get() {
    if (route.value.projectSlug) return route.value.projectSlug;
    if (route.value.name === "submission") return detailProject.value;
    return "";
  },
  set(value) {
    detailProject.value = value || "";
  },
});

function navigate(path) {
  if (path === locationPath.value) return;
  window.history.pushState({}, "", path);
  locationPath.value = window.location.pathname;
}

function openEditor(projectSlug) {
  navigate(`/projects/${encodeURIComponent(projectSlug)}`);
}

function openSubmission(submission) {
  const id = typeof submission === "string" ? submission : submission.submission_id || submission.id;
  navigate(`/submissions/${encodeURIComponent(id)}`);
}

function syncRoute() {
  locationPath.value = window.location.pathname;
}

async function resolveProjectSubmitAccess(slug = activeProject.value) {
  const generation = ++projectAccessGeneration;
  if (!slug) {
    projectSubmitState.value = "none";
    return;
  }
  if (!sessionReady.value || !sessionCapabilities.value.includes("submission.create")) {
    projectSubmitState.value = "denied";
    return;
  }
  projectSubmitState.value = "loading";
  try {
    const context = await getSubmissionContext(slug);
    if (generation !== projectAccessGeneration) return;
    projectSubmitState.value = context.actions_allowed?.includes("submission.create") ? "allowed" : "denied";
  } catch (_error) {
    if (generation === projectAccessGeneration) projectSubmitState.value = "denied";
  }
}

function handleProjectLoaded(projectSlug) {
  activeProject.value = projectSlug;
  void resolveProjectSubmitAccess(projectSlug);
}

watch(route, () => {
  dirty.value = false;
  detailProject.value = "";
});

watch(
  () => [sessionReady.value, activeProject.value, sessionCapabilities.value.includes("submission.create")],
  () => { void resolveProjectSubmitAccess(); },
  { immediate: true },
);

async function bootstrapSession() {
  sessionError.value = "";
  sessionReady.value = false;
  try {
    const session = await getSession();
    sessionCapabilities.value = session.capabilities || [];
    sessionReady.value = true;
  } catch (error) {
    sessionCapabilities.value = [];
    sessionError.value = error?.message || "No se pudo iniciar la sesión.";
  }
}

onMounted(() => {
  window.addEventListener("popstate", syncRoute);
  bootstrapSession();
});

onBeforeUnmount(() => window.removeEventListener("popstate", syncRoute));
</script>

<style scoped>
.studio-app {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.studio-app__view {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.studio-app__bootstrap,
.studio-app__not-found {
  display: grid;
  justify-items: center;
  gap: 16px;
  padding: 64px 24px;
  color: var(--toolbox-text);
}
</style>
