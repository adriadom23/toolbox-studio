<template>
  <a
    v-if="linkable"
    :href="driverConsoleUrl(clean)"
    target="_blank"
    rel="noopener noreferrer"
    class="driver-link"
  >{{ clean }}</a>
  <span v-else>{{ clean || fallback }}</span>
</template>

<script setup>
// Enlace estándar de driver id a la consola de drivers (amconsole). Regla
// permanente del usuario (2026-08-13): usar este componente siempre que se
// muestre un driver / transporter id. Si el valor no parece un id (nombre
// de tienda, "No DA"…) se muestra como texto plano.
import { computed } from "vue";

import { driverConsoleUrl, looksLikeDriverId } from "../driverConsole";

const props = defineProps({
  id: { type: [String, Number], default: "" },
  fallback: { type: String, default: "—" },
});

const clean = computed(() => String(props.id ?? "").trim());
const linkable = computed(() => looksLikeDriverId(clean.value));
</script>

<style scoped>
.driver-link {
  color: var(--toolbox-accent);
  text-decoration: none;
}
.driver-link:hover,
.driver-link:focus-visible {
  text-decoration: underline;
}
</style>
