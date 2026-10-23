<template>
  <header class="studio-shell">
    <button class="studio-shell__wordmark" type="button" @click="emit('navigate', '/')">
      🛠️ {{ t("studio.shell.wordmark") }}
    </button>
    <nav class="studio-shell__nav" :aria-label="t('studio.shell.navigation')">
      <button class="studio-shell__link" type="button" @click="emit('navigate', '/')">
        {{ t("studio.shell.projects") }}
      </button>
      <button class="studio-shell__link" type="button" @click="emit('navigate', '/submissions')">
        {{ t("studio.shell.submissions") }}
      </button>
    </nav>
    <div class="studio-shell__actions">
      <span v-if="submitHint" id="studio-submit-hint" class="studio-shell__hint" role="status">{{ submitHint }}</span>
      <button
        type="button"
        class="tb-button tb-button--primary studio-shell__submit"
        :disabled="!canSubmit"
        :title="submitHint"
        :aria-describedby="submitHint ? 'studio-submit-hint' : undefined"
        @click="openSubmit"
      >
        📤 {{ t("studio.shell.submit") }}
      </button>
      <LanguageSwitcher />
      <ThemeSwitcher />
    </div>
  </header>

  <SubmitProjectDialog
    v-model="showSubmit"
    :name="activeProject || ''"
    :dirty="dirty"
    @open-submission="emit('open-submission', $event)"
  />
</template>

<script setup>
import { computed, ref } from "vue";

import LanguageSwitcher from "../shared/components/LanguageSwitcher.vue";
import ThemeSwitcher from "../shared/components/ThemeSwitcher.vue";
import { useI18n } from "../shared/i18n";
import SubmitProjectDialog from "./SubmitProjectDialog.vue";

const { t } = useI18n();
const props = defineProps({
  activeProject: { type: String, default: "" },
  dirty: { type: Boolean, default: false },
  currentPath: { type: String, default: "/" },
  capabilities: { type: Array, default: () => [] },
  projectSubmitState: { type: String, default: "none" },
});
const emit = defineEmits(["navigate", "open-submission"]);
const showSubmit = ref(false);

const canSubmit = computed(() => (
  Boolean(props.activeProject)
  && !props.dirty
  && props.capabilities.includes("submission.create")
  && props.projectSubmitState === "allowed"
));
const submitHint = computed(() => {
  if (!props.activeProject) return t("studio.shell.noProject");
  if (!props.capabilities.includes("submission.create")) return t("studio.shell.noSubmitCapability");
  if (props.projectSubmitState === "loading") return t("studio.shell.checkingProjectAccess");
  if (props.projectSubmitState !== "allowed") return t("studio.shell.noProjectSubmitAccess");
  if (props.dirty) return t("studio.shell.saveBeforeSubmit");
  return "";
});

function openSubmit() {
  if (canSubmit.value) showSubmit.value = true;
}
</script>

<style scoped>
.studio-shell {
  position: sticky;
  top: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 10px 18px;
  border-bottom: 1px solid rgb(var(--toolbox-border-rgb) / 60%);
  background: rgb(var(--toolbox-panel-rgb) / 94%);
  backdrop-filter: blur(10px) saturate(130%);
}

.studio-shell__wordmark,
.studio-shell__link {
  border: 0;
  background: transparent;
  color: var(--toolbox-text);
  font: inherit;
  cursor: pointer;
}

.studio-shell__wordmark {
  font-size: 17px;
  font-weight: 800;
  white-space: nowrap;
}

.studio-shell__nav {
  display: flex;
  gap: 4px;
}

.studio-shell__link {
  padding: 8px;
  border-radius: 8px;
  color: var(--toolbox-muted);
  font-weight: 700;
}

.studio-shell__link:hover,
.studio-shell__link:focus-visible {
  color: var(--toolbox-accent);
  background: var(--toolbox-accent-soft);
}

.studio-shell__actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}

.studio-shell__hint {
  max-width: 220px;
  color: var(--toolbox-muted);
  font-size: 11px;
  text-align: right;
}

.studio-shell__submit {
  white-space: nowrap;
}

@media (max-width: 760px) {
  .studio-shell {
    flex-wrap: wrap;
    gap: 6px;
  }

  .studio-shell__nav {
    order: 3;
    width: 100%;
  }

  .studio-shell__actions {
    gap: 5px;
  }

  .studio-shell__hint {
    max-width: 150px;
    font-size: 10px;
  }
}
</style>
