<template>
  <!-- Clickable header of a table sorted with useTableSort(): the button
       keeps it keyboard accessible, aria-sort announces the direction. -->
  <th :aria-sort="sorter.ariaSort(k)" :class="{ 'sort-th--on': sorter.state.key === k }">
    <button type="button" class="sort-th__btn" :title="t('common.sortTip')" @click="sorter.toggle(k)">
      <slot />
      <span class="sort-th__ind" aria-hidden="true">{{ sorter.indicator(k) || "↕" }}</span>
    </button>
  </th>
</template>

<script setup>
import { useI18n } from "../i18n";

defineProps({
  // Object returned by useTableSort().
  sorter: { type: Object, required: true },
  // Column key passed to the sorter's accessors.
  k: { type: String, required: true },
});

const { t } = useI18n();
</script>

<style scoped>
.sort-th__btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: inherit;
  cursor: pointer;
  white-space: nowrap;
}
.sort-th__btn:focus-visible {
  outline: 2px solid var(--toolbox-accent);
  outline-offset: 2px;
  border-radius: 3px;
}
.sort-th__ind {
  font-size: 0.72em;
  opacity: 0.35;
}
.sort-th--on .sort-th__ind,
.sort-th__btn:hover .sort-th__ind {
  opacity: 0.9;
}
th.num .sort-th__btn {
  flex-direction: row-reverse;
}
</style>
