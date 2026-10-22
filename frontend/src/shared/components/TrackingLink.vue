<template>
  <a
    v-if="linkable"
    :href="sccTrackingUrl(clean)"
    target="_blank"
    rel="noopener noreferrer"
    class="tracking-link"
  >{{ clean }}</a>
  <span v-else>{{ clean || fallback }}</span>
</template>

<script setup>
import { computed } from "vue";

import { looksLikeTrackingId, sccTrackingUrl } from "../sccTracking";

const props = defineProps({
  id: { type: [String, Number], default: "" },
  fallback: { type: String, default: "—" },
});

const clean = computed(() => String(props.id ?? "").trim());
const linkable = computed(() => looksLikeTrackingId(clean.value));
</script>

<style scoped>
.tracking-link {
  color: var(--toolbox-accent);
  text-decoration: none;
}
.tracking-link:hover,
.tracking-link:focus-visible {
  text-decoration: underline;
}
</style>
