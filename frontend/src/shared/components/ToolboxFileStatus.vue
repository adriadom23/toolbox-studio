<template>
  <div class="toolbox-status" :class="found ? 'toolbox-status--ok' : 'toolbox-status--missing'">
    <span class="toolbox-status__icon">{{ found ? '✓' : '✗' }}</span>
    <span class="toolbox-status__label">{{ label || t("components.fileStatus.defaultLabel") }}</span>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();

defineProps({
  label: { type: String, default: "" },
});

const found = ref(false);

onMounted(async () => {
  try {
    const resp = await fetch("/api/toolbox-file");
    const data = await resp.json();
    found.value = Boolean(data.found);
  } catch {
    found.value = false;
  }
});
</script>

<style scoped>
.toolbox-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
}
.toolbox-status--ok {
  background: rgb(29 154 99 / 12%);
  color: #1d9a63;
}
.toolbox-status--missing {
  background: rgb(194 59 59 / 12%);
  color: var(--toolbox-danger);
}
.toolbox-status__icon {
  font-size: 16px;
  font-weight: 700;
}
</style>
