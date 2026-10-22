<template>
  <label class="language-switcher" :aria-label="t('navbar.language')">
    <span class="language-switcher__flag" aria-hidden="true">{{ currentLocale.flag }}</span>
    <select :value="locale" @change="setLocale($event.target.value)">
      <option v-for="item in availableLocales" :key="item.code" :value="item.code">
        {{ item.name }}
      </option>
    </select>
    <svg class="language-switcher__chevron" viewBox="0 0 12 8" aria-hidden="true">
      <path d="M1 1.5 6 6.5l5-5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  </label>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../i18n";

const { t, availableLocales, locale, setLocale } = useI18n();

const currentLocale = computed(
  () => availableLocales.find((item) => item.code === locale.value) ?? availableLocales[0],
);
</script>

<style scoped>
.language-switcher {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
  padding: 0 34px 0 12px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 10px;
  color: var(--toolbox-text);
  background: rgb(var(--toolbox-panel-rgb) / 62%);
  backdrop-filter: blur(10px) saturate(130%);
  -webkit-backdrop-filter: blur(10px) saturate(130%);
  box-shadow: 0 10px 24px rgb(var(--toolbox-shadow-rgb) / 8%);
  cursor: pointer;
}

.language-switcher:hover {
  border-color: var(--toolbox-accent);
}

.language-switcher__flag {
  font-size: 18px;
  line-height: 1;
}

.language-switcher__chevron {
  position: absolute;
  right: 12px;
  top: 50%;
  width: 10px;
  height: 6px;
  color: var(--toolbox-muted);
  pointer-events: none;
  transform: translateY(-50%);
}

select {
  min-width: 108px;
  border: 0;
  color: var(--toolbox-text);
  background: transparent;
  font: inherit;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  outline: 0;
  appearance: none;
  -webkit-appearance: none;
}
</style>
