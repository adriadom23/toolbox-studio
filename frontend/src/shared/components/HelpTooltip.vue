<template>
  <span
    ref="anchor"
    class="tb-help"
    role="button"
    :aria-label="t('components.fieldInfo.help')"
    @mouseenter="show"
    @mouseleave="scheduleHide"
    @focusin="show"
    @focusout="scheduleHide"
    tabindex="0"
    :aria-describedby="visible ? tipId : undefined"
  >
    <slot>?</slot>
    <teleport to="body">
      <span
        v-if="visible"
        class="tb-help-tooltip"
        :class="{ 'tb-help-tooltip--rich': hasGuide }"
        :id="tipId"
        role="tooltip"
        :style="tooltipStyle"
        @mouseenter="cancelHide"
        @mouseleave="scheduleHide"
      >
        <!-- With a wiki guide the panel shows ONLY the wiki content (the
             numbered steps below); the plain ``text`` summary is the
             fallback for tools without a guide. -->
        <span v-if="!hasGuide" class="tb-help-tooltip__text">{{ text }}</span>
        <!-- Wiki guide: numbered steps with text and screenshots interleaved,
             mirroring the ToolBox Wiki DO sub-page layout. The panel accepts
             the pointer and scrolls so every step stays reachable. -->
        <span v-if="hasGuide" class="tb-help-guide">
          <span v-for="(step, idx) in guideSteps" :key="idx" class="tb-help-guide__step">
            <span class="tb-help-guide__head">
              <span class="tb-help-guide__num">{{ idx + 1 }}</span>
              <span class="tb-help-guide__title">{{ step.title }}</span>
            </span>
            <span class="tb-help-guide__text">{{ step.text }}</span>
            <img
              v-for="src in step.images"
              :key="src"
              :src="src"
              :alt="`${step.title} — ${t('components.fieldInfo.help')}`"
              class="tb-help-guide__img"
              loading="lazy"
            />
          </span>
        </span>
      </span>
    </teleport>
  </span>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref } from "vue";

import { useI18n } from "../i18n";
import { toolHelpGuides } from "../toolHelpImages";

const props = defineProps({
  text: {
    type: String,
    required: true,
  },
  // Optional wiki guide key (e.g. "dex"): renders the step-by-step walkthrough
  // from toolHelpGuides + tools.<guide>.wikiGuide.* below the summary text.
  guide: {
    type: String,
    default: "",
  },
});

const { t } = useI18n();

// Unique tooltip id per instance so multiple tooltips on one page never
// produce duplicate ids (aria-describedby must point at THIS instance).
let helpTooltipSeq = 0;
const tipId = `tb-help-tooltip-${++helpTooltipSeq}-${Math.random().toString(36).slice(2, 7)}`;

const visible = ref(false);
const anchor = ref(null);
const tooltipStyle = ref({ top: "0px", left: "0px" });

const guideSteps = computed(() => {
  const steps = toolHelpGuides[props.guide];
  if (!steps) return [];
  return steps.map((images, idx) => ({
    title: t(`tools.${props.guide}.wikiGuide.s${idx + 1}.title`),
    text: t(`tools.${props.guide}.wikiGuide.s${idx + 1}.text`),
    images,
  }));
});

const hasGuide = computed(() => guideSteps.value.length > 0);

// Delayed close so the pointer can cross the gap between the "?" and the
// panel (needed to scroll the guide) without it vanishing.
let hideTimer = null;

function cancelHide() {
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
}

function scheduleHide() {
  cancelHide();
  hideTimer = window.setTimeout(() => {
    visible.value = false;
  }, 180);
}

async function show() {
  cancelHide();
  visible.value = true;
  await nextTick();
  reposition();
}

function reposition() {
  if (!anchor.value) return;
  const rect = anchor.value.getBoundingClientRect();
  const maxWidth = hasGuide.value
    ? Math.min(720, window.innerWidth * 0.92)
    : Math.min(360, window.innerWidth * 0.8);
  const gap = 10;
  const margin = 14;
  let top = rect.bottom + gap;
  // Shift to the left so the tooltip aligns to the anchor's right edge.
  let left = rect.right - maxWidth;
  if (left < 12) left = 12;
  const style = {
    top: `${top}px`,
    left: `${left}px`,
    width: `${maxWidth}px`,
  };
  if (hasGuide.value) {
    // Rich panel: cap to the space below the anchor and scroll inside.
    style.maxHeight = `${Math.max(260, window.innerHeight - top - margin)}px`;
  } else if (top + 120 > window.innerHeight) {
    style.top = `${rect.top - 120 - gap}px`;
  }
  tooltipStyle.value = style;
}

onBeforeUnmount(() => {
  cancelHide();
  visible.value = false;
});
</script>

<style scoped>
.tb-help {
  position: relative;
  display: inline-grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 76%);
  border-radius: 50%;
  color: var(--toolbox-accent);
  background: rgb(var(--toolbox-panel-rgb) / 58%);
  font-weight: 800;
  cursor: help;
  user-select: none;
}

.tb-help:hover,
.tb-help:focus {
  border-color: var(--toolbox-accent);
  outline: 0;
}
</style>

<style>
/* Global (not scoped) so it works when teleported to <body>. */
.tb-help-tooltip {
  position: fixed;
  z-index: 10050;
  padding: 10px 12px;
  border-radius: 8px;
  color: var(--toolbox-tooltip-fg);
  background: var(--toolbox-tooltip-bg);
  font-family: var(--tb-font-body);
  font-size: 12px;
  font-weight: 500;
  line-height: 1.45;
  text-transform: none;
  box-shadow: 0 16px 40px rgb(var(--toolbox-shadow-rgb) / 22%);
  pointer-events: none;
  animation: tbHelpFade 0.12s ease-out;
}

.tb-help-tooltip__text {
  /* Multi-section tooltips (e.g. the Extracción/Transformación/Envío
     copy mirrored from the ToolBox Wiki) author line breaks with "\n". */
  white-space: pre-line;
}

/* Rich variant (wiki step guide): wide, interactive, scrollable. Unlike the
   small hint tooltips (which invert colours on purpose), this panel follows
   the app theme — dark card in dark mode, light card in light mode — via the
   theme tokens, which already react to [data-theme]. */
.tb-help-tooltip--rich {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px 18px;
  font-size: 12.5px;
  color: var(--toolbox-text);
  background: var(--toolbox-card);
  border: 1px solid rgb(var(--toolbox-border-rgb) / 70%);
  overflow-y: auto;
  overscroll-behavior: contain;
  pointer-events: auto;
}

.tb-help-guide {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tb-help-guide__step {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tb-help-guide__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tb-help-guide__num {
  flex-shrink: 0;
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--toolbox-accent);
  /* Panel token = near-white in light theme, deep navy in dark theme, so the
     digit keeps contrast against the accent circle in both. */
  color: var(--toolbox-panel);
  font-size: 11px;
  font-weight: 800;
}

.tb-help-guide__title {
  font-weight: 700;
  font-size: 13px;
}

.tb-help-guide__text {
  white-space: pre-line;
  color: inherit;
}

.tb-help-guide__img {
  display: block;
  width: 100%;
  height: auto;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 60%);
  border-radius: 8px;
  background: #fff;
}

@keyframes tbHelpFade {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
