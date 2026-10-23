<template>
  <span class="status-badge" :class="`status-badge--${safeStatus}`">
    {{ label }}
  </span>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../shared/i18n";

const { t } = useI18n();
const props = defineProps({ status: { type: String, default: "unknown" } });
const safeStatus = computed(() => props.status.replace(/[^a-z_]/g, ""));
const label = computed(() => t(`studio.status.${safeStatus.value}`));
</script>

<style scoped>
.status-badge { display: inline-flex; padding: 4px 9px; border: 1px solid rgb(var(--toolbox-border-rgb) / 70%); border-radius: 999px; color: var(--toolbox-muted); background: rgb(var(--toolbox-surface-rgb) / 55%); font-size: 11px; font-weight: 800; text-transform: uppercase; }
.status-badge--submitted, .status-badge--published { color: var(--toolbox-success); border-color: var(--toolbox-success); }
.status-badge--under_review, .status-badge--approved { color: var(--toolbox-accent); border-color: var(--toolbox-accent); }
.status-badge--changes_requested, .status-badge--building { color: var(--toolbox-warning); border-color: var(--toolbox-warning); }
.status-badge--rejected, .status-badge--build_failed { color: var(--toolbox-danger); border-color: var(--toolbox-danger); }
</style>
