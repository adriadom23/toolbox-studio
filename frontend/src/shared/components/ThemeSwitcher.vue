<template>
  <button
    type="button"
    class="theme-switcher"
    :aria-label="label"
    :title="label"
    :aria-pressed="isDark"
    @click="toggleTheme"
  >
    <svg
      v-if="isDark"
      class="theme-switcher__icon"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path
        d="M20.7 14.6a8.5 8.5 0 0 1-11.3-11A1 1 0 0 0 8 2.4a10.5 10.5 0 1 0 13.6 13.6 1 1 0 0 0-1-1.4Z"
        fill="currentColor"
      />
    </svg>
    <svg
      v-else
      class="theme-switcher__icon"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <g
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <circle cx="12" cy="12" r="4.2" fill="currentColor" stroke="none" />
        <path d="M12 2.5v2.4" />
        <path d="M12 19.1v2.4" />
        <path d="M2.5 12h2.4" />
        <path d="M19.1 12h2.4" />
        <path d="m4.9 4.9 1.7 1.7" />
        <path d="m17.4 17.4 1.7 1.7" />
        <path d="m4.9 19.1 1.7-1.7" />
        <path d="m17.4 6.6 1.7-1.7" />
      </g>
    </svg>
  </button>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../i18n";
import { useTheme } from "../theme";

const { t } = useI18n();
const { isDark, toggleTheme } = useTheme();

const label = computed(() =>
  isDark.value ? t("common.theme.toLight") : t("common.theme.toDark"),
);
</script>

<style scoped>
.theme-switcher {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  padding: 0;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 10px;
  color: var(--toolbox-text);
  background: rgb(var(--toolbox-panel-rgb) / 62%);
  backdrop-filter: blur(10px) saturate(130%);
  -webkit-backdrop-filter: blur(10px) saturate(130%);
  box-shadow: 0 10px 24px rgb(var(--toolbox-shadow-rgb) / 8%);
  cursor: pointer;
  transition:
    background 150ms ease,
    border-color 150ms ease,
    color 150ms ease,
    transform 150ms ease;
}

.theme-switcher:hover {
  border-color: var(--toolbox-accent);
  color: var(--toolbox-accent);
  transform: translateY(-1px);
}

.theme-switcher:focus-visible {
  outline: 2px solid var(--toolbox-accent);
  outline-offset: 2px;
}

.theme-switcher__icon {
  width: 20px;
  height: 20px;
}
</style>
