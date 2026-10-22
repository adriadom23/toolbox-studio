<template>
  <a
    v-if="href"
    :href="href"
    target="_blank"
    rel="noopener noreferrer"
    @click.stop
    @dblclick.stop
    @keydown.stop
  ><slot>{{ routeCode }}</slot></a>
  <span v-else><slot>{{ routeCode }}</slot></span>
</template>

<script setup>
import { computed } from "vue";

import { cortexRouteUrl } from "../cortexRouteUrl";

const props = defineProps({
  planId: { type: [String, Number], default: "" },
  routeCode: { type: String, default: "" },
  date: { type: String, default: "" },
  serviceAreaId: { type: String, default: "" },
});

const href = computed(() => cortexRouteUrl(props));
</script>
