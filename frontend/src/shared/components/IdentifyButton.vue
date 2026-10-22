<template>
  <button
    type="button"
    class="tb-button"
    :class="allConnected ? 'tb-button--success' : 'tb-button--primary'"
    :disabled="anyReconnecting"
    :title="title"
    @click="onClick"
  >
    <span aria-hidden="true">{{ allConnected ? "✓" : "🔐" }}</span>
    {{ label }}
  </button>
</template>

<script setup>
import { computed } from "vue";

import { useI18n } from "../i18n";

/**
 * Single "Reconectar" button wired to a ``useIdentity`` manager. It re-probes
 * every source and reconnects the ones that are not currently connected (a
 * live session is never bounced). The label is deliberately plain — no
 * pending count, ever (user request, 2026-08-26):
 *   - all connected  → green "✓ Todo conectado" (re-probes on click)
 *   - reconnecting    → disabled "Reconectando…"
 *   - otherwise       → "Reconectar"
 */
const props = defineProps({
  /** The object returned by ``useIdentity(...)``. */
  identity: {
    type: Object,
    required: true,
  },
  /**
   * Optional tool-level readiness gate: when provided, the green
   * "✓ Todo conectado" state additionally requires it. Tools pass their
   * own "every chip is green" computed so the button never claims all
   * connected while a chip is still connecting/loading data.
   */
  ready: {
    type: Boolean,
    default: null,
  },
});

const { t } = useI18n();

const allConnected = computed(() => {
  const base = Boolean(props.identity.allConnected.value);
  return props.ready === null ? base : base && props.ready;
});
const anyReconnecting = computed(() => props.identity.anyReconnecting.value);

const label = computed(() => {
  if (anyReconnecting.value) return t("common.reconnecting");
  if (allConnected.value) return t("common.allConnected");
  return t("common.reconnect");
});

const title = computed(() => label.value);

function onClick() {
  props.identity.identify();
}
</script>
