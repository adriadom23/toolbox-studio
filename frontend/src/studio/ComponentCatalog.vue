<template>
  <div class="catalog tool-surface">
    <header class="catalog__head">
      <div>
        <h1 class="catalog__title">{{ t("studio.catalog.title") }}</h1>
        <p class="catalog__subtitle">{{ t("studio.catalog.subtitle", { count: componentCatalog.length }) }}</p>
      </div>
      <button type="button" class="tb-button tb-button--ghost" @click="emit('back')">
        {{ t("studio.catalog.back") }}
      </button>
    </header>

    <ul class="catalog__grid">
      <li v-for="entry in componentCatalog" :key="entry.name" class="catalog-card">
        <div class="catalog-card__head">
          <h2 class="catalog-card__name">{{ entry.name }}</h2>
        </div>
        <p class="catalog-card__desc">{{ entry.description }}</p>

        <p class="catalog-card__import">
          <span class="catalog-card__label">{{ t("studio.catalog.importLabel") }}</span>
          <code>import {{ entry.name }} from "{{ entry.importPath }}";</code>
        </p>

        <div v-if="entry.live" class="catalog-card__preview">
          <span class="catalog-card__label">{{ t("studio.catalog.livePreview") }}</span>
          <CatalogLiveExample :entry="entry" :identity="identity" :sorter="sorter" />
        </div>

        <div class="catalog-card__example">
          <div class="catalog-card__example-head">
            <span class="catalog-card__label">{{ t("studio.catalog.exampleLabel") }}</span>
            <CopyBlocksButtons
              :items="[entry.example]"
              :label="t('studio.catalog.copy')"
              variant="ghost"
              button-class="catalog-card__copy"
            />
          </div>
          <pre class="catalog-card__code"><code>{{ entry.example }}</code></pre>
        </div>
      </li>

      <!-- Documentation-only card for the connections composable pair. -->
      <li class="catalog-card catalog-card--doc">
        <div class="catalog-card__head">
          <h2 class="catalog-card__name">{{ connectionsDoc.name }}</h2>
        </div>
        <p class="catalog-card__desc">{{ connectionsDoc.description }}</p>
        <p class="catalog-card__import">
          <span class="catalog-card__label">{{ t("studio.catalog.importLabel") }}</span>
          <code>import {{ connectionsDoc.importPath }}</code>
        </p>
        <div class="catalog-card__example">
          <div class="catalog-card__example-head">
            <span class="catalog-card__label">{{ t("studio.catalog.exampleLabel") }}</span>
            <CopyBlocksButtons
              :items="[connectionsDoc.example]"
              :label="t('studio.catalog.copy')"
              variant="ghost"
              button-class="catalog-card__copy"
            />
          </div>
          <pre class="catalog-card__code"><code>{{ connectionsDoc.example }}</code></pre>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { useI18n } from "../shared/i18n";
import { useIdentity } from "../shared/stores/useIdentity";
import { useTableSort } from "../shared/useTableSort";
import CopyBlocksButtons from "../shared/components/CopyBlocksButtons.vue";
import CatalogLiveExample from "./CatalogLiveExample.vue";
import { componentCatalog, connectionsDoc } from "./componentCatalogData";

const { t } = useI18n();
const emit = defineEmits(["back"]);

// One shared useIdentity() mock for every identity-based live example. It
// self-probes /api/connections/*/status on mount, which gracefully reports
// "disconnected" in this environment (no live sessions) — exactly the state
// the chips demonstrate. useIdentity has NO probeAll(); nothing is called
// here to start it (see the connectionsDoc entry).
const identity = useIdentity([
  { id: "scc", label: "SCC" },
  { id: "mercury", label: "Mercury" },
]);

// One shared useTableSort() for the SortTh live example.
const sorter = useTableSort({ id: (row) => row.id });
</script>

<style scoped>
.catalog {
  max-width: 1320px;
  margin: 0 auto;
  padding: 24px;
}

.catalog__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 22px;
}

.catalog__title {
  margin: 0;
  font-family: var(--tb-font-heading);
  font-size: 24px;
  color: var(--toolbox-text);
}

.catalog__subtitle {
  margin: 4px 0 0;
  color: var(--toolbox-muted);
  font-size: 14px;
}

.catalog__grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 16px;
}

.catalog-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 55%);
  border-radius: 14px;
  padding: 16px;
  background: rgb(var(--toolbox-surface-rgb) / 40%);
}

.catalog-card--doc {
  border-style: dashed;
}

.catalog-card__name {
  margin: 0;
  font-size: 16px;
  color: var(--toolbox-text);
  font-family: var(--tb-font-mono, monospace);
}

.catalog-card__desc {
  margin: 0;
  font-size: 13px;
  color: var(--toolbox-muted);
}

.catalog-card__label {
  display: block;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--toolbox-muted);
  margin-bottom: 4px;
}

.catalog-card__import code {
  display: block;
  font-size: 11.5px;
  word-break: break-all;
  color: var(--toolbox-text);
}

.catalog-card__preview {
  padding: 10px;
  border: 1px dashed rgb(var(--toolbox-border-rgb) / 45%);
  border-radius: 10px;
  background: rgb(var(--toolbox-panel-rgb) / 30%);
}

.catalog-card__example {
  margin-top: auto;
}

.catalog-card__example-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.catalog-card__code {
  margin: 6px 0 0;
  padding: 10px;
  border-radius: 10px;
  background: rgb(12 18 24 / 72%);
  color: #e6edf3;
  font-size: 11.5px;
  line-height: 1.5;
  overflow: auto;
  white-space: pre;
}

:deep(.catalog-card__copy) {
  font-size: 11px;
  min-height: 28px;
}
</style>
