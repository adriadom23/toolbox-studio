<template>
  <Modal
    :model-value="modelValue"
    :title="toolLabel || t('destinos.picker.title')"
    :eyebrow="t('destinos.picker.eyebrow')"
    :max-width="560"
    @update:modelValue="(v) => emit('update:modelValue', v)"
  >
    <p class="picker-hint">{{ t("destinos.picker.hint") }}</p>

    <!-- Medium tabs: Slack channels vs Email mailboxes -->
    <nav class="picker-tabs">
      <button
        type="button"
        class="picker-tab"
        :class="{ active: activeTab === 'slack' }"
        @click="activeTab = 'slack'"
      >
        <span class="picker-tab__icon picker-tab__icon--slack" aria-hidden="true">#</span>
        {{ t("destinos.picker.tabSlack") }}
        <span class="picker-tab__count">{{ slackSelectedCount }}/{{ slackCatalog.length }}</span>
      </button>
      <button
        type="button"
        class="picker-tab"
        :class="{ active: activeTab === 'email' }"
        @click="activeTab = 'email'"
      >
        <span class="picker-tab__icon picker-tab__icon--email" aria-hidden="true">✉</span>
        {{ t("destinos.picker.tabEmail") }}
        <span class="picker-tab__count">{{ emailSelectedCount }}/{{ emailCatalog.length }}</span>
      </button>
    </nav>

    <ul class="picker-list">
      <li v-for="chan in visibleCatalog" :key="chan.code" class="picker-item">
        <label class="picker-option" :class="{ 'is-on': selectedSet.has(chan.code) }">
          <input
            type="checkbox"
            :checked="selectedSet.has(chan.code)"
            @change="toggle(chan.code)"
          />
          <span class="picker-option__medium" :class="`picker-option__medium--${chan.medium}`" aria-hidden="true">
            {{ chan.medium === "email" ? "✉" : "#" }}
          </span>
          <span class="picker-option__label">{{ chan.label }}</span>
        </label>
      </li>
    </ul>

    <p v-if="!visibleCatalog.length" class="picker-empty">{{ t("destinos.picker.emptyTab") }}</p>
    <p v-else-if="!local.length" class="picker-empty">{{ t("destinos.picker.none") }}</p>

    <label class="picker-poc" :class="{ 'is-on': pocConfirmed }">
      <input type="checkbox" v-model="pocConfirmed" />
      <span>{{ t("destinos.picker.pocConfirm") }}</span>
    </label>
    <p class="picker-shared">{{ t("destinos.picker.sharedHint") }}</p>

    <template #footer>
      <button type="button" class="tb-button tb-button--ghost" :disabled="saving" @click="cancel">
        {{ t("destinos.picker.cancel") }}
      </button>
      <button
        type="button"
        class="tb-button tb-button--primary"
        :disabled="saving || !pocConfirmed"
        :title="!pocConfirmed ? t('destinos.picker.pocRequired') : ''"
        @click="save"
      >
        {{ saving ? t("common.processing") : t("destinos.picker.save") }}
      </button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, ref, watch } from "vue";

import Modal from "./Modal.vue";
import { useI18n } from "../i18n";

const { t } = useI18n();

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  toolLabel: { type: String, default: "" },
  catalog: { type: Array, default: () => [] },
  selected: { type: Array, default: () => [] },
  saving: { type: Boolean, default: false },
});

const emit = defineEmits(["update:modelValue", "save"]);

const local = ref([]);
const pocConfirmed = ref(false);
const activeTab = ref("slack");

// Reset the local selection (and POC confirmation) each time it opens.
watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      local.value = [...props.selected];
      pocConfirmed.value = false;
      activeTab.value = "slack";
    }
  },
  { immediate: true },
);

const selectedSet = computed(() => new Set(local.value));

// Split the catalog by medium so each tab shows only its destinations.
const slackCatalog = computed(() =>
  props.catalog.filter((c) => c.medium !== "email"),
);
const emailCatalog = computed(() => props.catalog.filter((c) => c.medium === "email"));

const visibleCatalog = computed(() =>
  activeTab.value === "email" ? emailCatalog.value : slackCatalog.value,
);

const slackSelectedCount = computed(
  () => slackCatalog.value.filter((c) => selectedSet.value.has(c.code)).length,
);
const emailSelectedCount = computed(
  () => emailCatalog.value.filter((c) => selectedSet.value.has(c.code)).length,
);

function toggle(code) {
  if (selectedSet.value.has(code)) {
    local.value = local.value.filter((c) => c !== code);
  } else {
    local.value = [...local.value, code];
  }
}

function cancel() {
  emit("update:modelValue", false);
}

function save() {
  if (!pocConfirmed.value) return;
  // Persist in catalog order for a stable, predictable layout.
  const order = props.catalog.map((c) => c.code);
  const ordered = order.filter((c) => local.value.includes(c));
  emit("save", ordered);
}
</script>

<style scoped>
.picker-hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--toolbox-text-muted);
}

.picker-tabs {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
}

.picker-tab {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 1;
  justify-content: center;
  border: 1px solid rgba(0, 0, 0, 0.12);
  background: rgba(0, 0, 0, 0.02);
  border-radius: 9px;
  padding: 7px 12px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  color: var(--toolbox-text);
  transition: border-color 0.15s ease, background 0.15s ease;
}

.picker-tab:hover {
  border-color: var(--toolbox-accent);
}

.picker-tab.active {
  border-color: var(--toolbox-accent);
  background: rgba(139, 92, 246, 0.1);
}

.picker-tab__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
}

.picker-tab__icon--slack {
  background: rgba(59, 130, 246, 0.15);
  color: #1d4ed8;
}

.picker-tab__icon--email {
  background: rgba(245, 158, 11, 0.18);
  color: #b45309;
}

.picker-tab__count {
  font-size: 11px;
  font-weight: 700;
  color: var(--toolbox-text-muted);
  background: rgba(0, 0, 0, 0.06);
  border-radius: 999px;
  padding: 1px 7px;
}

.picker-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
}

.picker-option {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 9px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  background: rgba(0, 0, 0, 0.02);
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.picker-option:hover {
  border-color: var(--toolbox-accent);
}

.picker-option.is-on {
  border-color: var(--toolbox-accent);
  background: rgba(139, 92, 246, 0.08);
}

.picker-option input {
  width: 16px;
  height: 16px;
  accent-color: var(--toolbox-accent);
}

.picker-option__medium {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  background: rgba(59, 130, 246, 0.15);
  color: #1d4ed8;
}

.picker-option__medium--email {
  background: rgba(245, 158, 11, 0.18);
  color: #b45309;
}

.picker-option__label {
  font-weight: 600;
  font-size: 13.5px;
  color: var(--toolbox-text);
  flex: 1;
}

.picker-empty {
  margin: 12px 0 0;
  font-size: 12px;
  font-style: italic;
  color: var(--toolbox-text-muted);
}

.picker-poc {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  margin: 16px 0 6px;
  padding: 10px 12px;
  border-radius: 9px;
  border: 1px solid rgba(196, 61, 61, 0.28);
  background: rgba(196, 61, 61, 0.06);
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--toolbox-text);
}

.picker-poc.is-on {
  border-color: rgba(46, 182, 125, 0.4);
  background: rgba(46, 182, 125, 0.08);
}

.picker-poc input {
  margin-top: 2px;
  width: 16px;
  height: 16px;
  accent-color: var(--toolbox-accent);
}

.picker-shared {
  margin: 0;
  font-size: 11.5px;
  color: var(--toolbox-text-muted);
}

/* Dark theme: variantes legibles de los colores informativos fijos */
[data-theme="dark"] .picker-tab__icon--slack {
  color: #82a9d8;
}
[data-theme="dark"] .picker-option__medium {
  color: #82a9d8;
}
</style>
