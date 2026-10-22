<template>
  <header class="studio-shell">
    <span class="studio-shell__wordmark">🛠️ Toolbox Studio</span>
    <div class="studio-shell__actions">
      <LanguageSwitcher />
      <ThemeSwitcher />
    </div>
  </header>
</template>

<script setup>
// Minimal top bar (design.md §5.1): a wordmark plus the vendored
// ThemeSwitcher.vue and LanguageSwitcher.vue, nothing else — no
// navigation, no tool links, no station picker (Studio has none of those
// concepts).
import ThemeSwitcher from "../shared/components/ThemeSwitcher.vue";
import LanguageSwitcher from "../shared/components/LanguageSwitcher.vue";
</script>

<style scoped>
.studio-shell {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 24px;
  border-bottom: 1px solid rgb(var(--toolbox-border-rgb) / 60%);
  background: rgb(var(--toolbox-panel-rgb) / 70%);
  backdrop-filter: blur(10px) saturate(130%);
  -webkit-backdrop-filter: blur(10px) saturate(130%);
}

.studio-shell__wordmark {
  font-size: 17px;
  font-weight: 700;
  color: var(--toolbox-text);
  letter-spacing: 0.01em;
}

.studio-shell__actions {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}
</style>
