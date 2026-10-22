<template>
  <div class="studio-app">
    <StudioShell />
    <main class="studio-app__view">
      <ProjectList v-if="currentView === 'list'" @open="openEditor" />
      <ProjectEditor
        v-else
        :name="currentView.editor"
        @back="showList"
      />
    </main>
  </div>
</template>

<script setup>
// Router-less view switch (design.md §5.2): a single ref holds either the
// literal "list" or {editor: projectName}, and a plain v-if decides which
// screen renders. Consistent with ToolBox's own "no router/state library"
// convention and proportionate to Studio's actual surface — two screens.
import { ref } from "vue";

import StudioShell from "./studio/StudioShell.vue";
import ProjectList from "./studio/ProjectList.vue";
import ProjectEditor from "./studio/ProjectEditor.vue";

const currentView = ref("list");

function openEditor(name) {
  currentView.value = { editor: name };
}

function showList() {
  currentView.value = "list";
}
</script>

<style scoped>
.studio-app {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.studio-app__view {
  flex: 1;
}
</style>
