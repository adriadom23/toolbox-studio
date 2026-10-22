<template>
  <div class="catalog-live">
    <p v-if="failed" class="catalog-live__error">{{ errorText }}</p>

    <!-- Modal entries: a trigger button that opens the real modal so the
         teleport/focus-trap behaviour is exercised without a permanently
         open overlay covering the catalog. -->
    <template v-else-if="entry.isModal">
      <button type="button" class="tb-button tb-button--primary" @click="modalOpen = true">
        {{ t("studio.catalog.livePreview") }} ▸
      </button>
      <component :is="entry.component" v-model="modalOpen" v-bind="entry.liveProps || {}">
        <span>{{ entry.liveSlot || "…" }}</span>
        <template #footer>
          <button type="button" class="tb-button tb-button--ghost" @click="modalOpen = false">
            {{ t("common.close") }}
          </button>
        </template>
      </component>
    </template>

    <!-- SortTh must live inside a table row to be valid markup. -->
    <table v-else-if="entry.wrapInTable" class="tb-table catalog-live__table" data-no-dblsort>
      <thead>
        <tr>
          <component :is="entry.component" :sorter="sorter" k="id">{{ entry.liveSlot || "ID" }}</component>
        </tr>
      </thead>
      <tbody>
        <tr><td>1</td></tr>
        <tr><td>2</td></tr>
      </tbody>
    </table>

    <!-- Everything else: render the component directly with placeholder props
         (and the shared mock identity when it needs one). -->
    <component
      :is="entry.component"
      v-else
      v-bind="boundProps"
    >
      <span v-if="entry.liveSlot">{{ entry.liveSlot }}</span>
    </component>
  </div>
</template>

<script setup>
import { onErrorCaptured, ref } from "vue";

import { useI18n } from "../shared/i18n";

const { t } = useI18n();

const props = defineProps({
  entry: { type: Object, required: true },
  // A shared useIdentity() mock, passed down so identity-based components
  // (ConnStatusChips/IdentifyButton/IdentifyPanel) all share one instance.
  identity: { type: Object, default: null },
  // A shared useTableSort() instance for SortTh.
  sorter: { type: Object, default: null },
});

const modalOpen = ref(false);
const failed = ref(false);
const errorText = ref("");

// Compose the props bound on a plain (non-modal, non-table) live instance:
// its declared placeholder props plus the identity (and any identity-specific
// extra props the entry declares, e.g. IdentifyPanel's show-status).
const boundProps = {
  ...(props.entry.liveProps || {}),
  ...(props.entry.needsIdentity ? { identity: props.identity } : {}),
  ...(props.entry.needsIdentity ? props.entry.identityProps || {} : {}),
};

// Error boundary: a placeholder-prop mismatch in any single live example must
// never break the whole catalog (acceptance: all 34 entries render). A caught
// error swaps that one preview for a short notice; the snippet stays visible.
onErrorCaptured((err) => {
  failed.value = true;
  errorText.value = `⚠ ${err?.message || String(err)}`;
  return false;
});
</script>

<style scoped>
.catalog-live {
  min-height: 24px;
}

.catalog-live__error {
  margin: 0;
  font-size: 12px;
  color: var(--toolbox-warning, #b8860b);
}

.catalog-live__table {
  width: auto;
}
</style>
