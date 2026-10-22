<template>
  <!-- Floating window used to open quick messaging tools (Mensaje Libre,
       Mensajes XMDO, Perfo/PNOV) on top of the tool currently running,
       instead of navigating away from it. Centered over a dimmed backdrop
       with the same look as the shared Modal (tb-modal), but with its own
       z-index below 10000 so any modal the popped tool opens (XMDO
       actions, OTR preview, etc.) still renders above this window. -->
  <Teleport to="body">
    <div
      class="tool-popup-window"
      :style="popupEntryStyle"
      :class="{ 'tool-popup-window--max': maximized }"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
      @click.self="$emit('close')"
    >
      <div ref="shell" class="tool-popup-window__shell" tabindex="-1">
        <header class="tool-popup-window__bar">
          <div class="tool-popup-window__heading">
            <p v-if="eyebrow" class="tool-popup-window__eyebrow">{{ eyebrow }}</p>
            <strong class="tool-popup-window__title">{{ title }}</strong>
          </div>
          <div class="tool-popup-window__actions">
            <!-- Maximize toggle (opt-in via the ``maximizable`` prop):
                 stretches the window to the FULL screen so only its
                 content is visible — nothing else behind it. -->
            <button
              v-if="maximizable"
              type="button"
              class="tool-popup-window__close"
              :aria-label="maximized ? t('common.popupRestore') : t('common.popupMaximize')"
              :title="maximized ? t('common.popupRestore') : t('common.popupMaximize')"
              @click="maximized = !maximized"
            >
              <svg v-if="!maximized" viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
                <path
                  d="M3 8V3h5v2H5v3H3zm14-5v5h-2V5h-3V3h5zM3 12h2v3h3v2H3v-5zm12 3v-3h2v5h-5v-2h3z"
                  fill="currentColor"
                />
              </svg>
              <svg v-else viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
                <path
                  d="M8 3v5H3V6h3V3h2zm9 3v2h-5V3h2v3h3zM3 12h5v5H6v-3H3v-2zm11 3v-3h3v2h-3v3h-2v-2h2z"
                  fill="currentColor"
                />
              </svg>
            </button>
            <button
              type="button"
              class="tool-popup-window__close"
              :aria-label="t('common.close')"
              :title="t('common.close')"
              @click="$emit('close')"
            >
              <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
                <path
                  d="M5.3 4 4 5.3 8.7 10 4 14.7 5.3 16 10 11.3 14.7 16 16 14.7 11.3 10 16 5.3 14.7 4 10 8.7 5.3 4z"
                  fill="currentColor"
                />
              </svg>
            </button>
          </div>
        </header>

        <div class="tool-popup-window__body">
          <slot />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";

import { useI18n } from "../i18n";
import { randomEntryMotion } from "../entryMotion";

defineProps({
  // Localized name of the tool shown in the window bar.
  title: { type: String, default: "" },
  // Small label above the title (e.g. the tool's shift).
  eyebrow: { type: String, default: "" },
  // Show the maximize toggle in the header bar. When maximized the
  // window covers the ENTIRE screen (only its content is visible).
  maximizable: { type: Boolean, default: false },
});

const emit = defineEmits(["close"]);

// Fullscreen state of the window (only reachable when ``maximizable``).
const maximized = ref(false);
const shell = ref(null);
const popupEntryStyle = randomEntryMotion("popup");
let previousActiveElement = null;

const { t } = useI18n();

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusableElements(root = shell.value) {
  if (!root) return [];
  return [...root.querySelectorAll(FOCUSABLE_SELECTOR)].filter((element) => {
    const style = window.getComputedStyle(element);
    return (
      !element.hasAttribute('hidden') &&
      element.getAttribute('aria-hidden') !== 'true' &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      element.getClientRects().length > 0
    );
  });
}

function isTopPopup() {
  if (!shell.value) return false;
  const popups = [...document.querySelectorAll('.tool-popup-window')].filter(
    (popup) => popup.getClientRects().length > 0,
  );
  return popups.at(-1) === shell.value.closest('.tool-popup-window');
}

// Escape closes only the top floating window. Tab is trapped inside that
// window so nested popups cannot close together or leak focus to the page.
function onKeydown(event) {
  if (!isTopPopup() || document.body.classList.contains("tb-modal-open")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopImmediatePropagation();
    emit("close");
    return;
  }
  if (event.key !== "Tab") return;

  const focusable = focusableElements();
  if (!focusable.length) {
    event.preventDefault();
    event.stopImmediatePropagation();
    shell.value?.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (
    active === shell.value ||
    active === first ||
    !shell.value?.contains(active)
  )) {
    event.preventDefault();
    event.stopImmediatePropagation();
    last.focus();
  } else if (!event.shiftKey && (active === last || !shell.value?.contains(active))) {
    event.preventDefault();
    event.stopImmediatePropagation();
    first.focus();
  }
}

onMounted(() => {
  previousActiveElement = document.activeElement;
  window.addEventListener("keydown", onKeydown, true);
  nextTick(() => shell.value?.focus({ preventScroll: true }));
});

onBeforeUnmount(() => {
  const shouldRestoreFocus =
    isTopPopup() && !document.body.classList.contains("tb-modal-open");
  window.removeEventListener("keydown", onKeydown, true);
  if (shouldRestoreFocus && previousActiveElement?.isConnected) {
    previousActiveElement.focus({ preventScroll: true });
  }
});
</script>

<style scoped>
.tool-popup-window {
  position: fixed;
  inset: 0;
  z-index: 9500;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgb(12 18 24 / 64%);
  backdrop-filter: blur(6px) saturate(130%);
  -webkit-backdrop-filter: blur(6px) saturate(130%);
  animation: tool-popup-backdrop-enter var(--tb-entry-backdrop-duration, 220ms)
    ease-out both;
}

.tool-popup-window__shell {
  width: min(1120px, 100%);
  min-width: min(560px, 100%);
  max-width: 100%;
  min-height: min(280px, calc(100dvh - 48px));
  max-height: calc(100vh - 48px);
  max-height: calc(100dvh - 48px);
  display: grid;
  grid-template-rows: auto 1fr;
  overflow: hidden;
  resize: both;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 60%);
  border-radius: 14px;
  background: rgb(var(--toolbox-panel-rgb) / 96%);
  color: var(--toolbox-text);
  box-shadow: 0 30px 80px rgb(12 18 24 / 38%);
  animation: tool-popup-shell-enter var(--tb-entry-duration, 300ms)
    cubic-bezier(0.16, 1, 0.3, 1) both;
}

@keyframes tool-popup-backdrop-enter {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes tool-popup-shell-enter {
  from {
    opacity: 0;
    transform:
      translate(var(--tb-entry-x, 0px), var(--tb-entry-y, 16px))
      scale(var(--tb-entry-scale, 0.96));
  }
  to {
    opacity: 1;
    transform: translate(0, 0) scale(1);
  }
}

@media (prefers-reduced-motion: reduce) {
  .tool-popup-window,
  .tool-popup-window__shell {
    animation: none;
  }
}

.tool-popup-window__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 18px;
  border-bottom: 1px solid rgb(var(--toolbox-border-rgb) / 56%);
}

.tool-popup-window__heading {
  min-width: 0;
}

.tool-popup-window__eyebrow {
  margin: 0;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--toolbox-muted);
}

.tool-popup-window__title {
  display: block;
  font-family: var(--tb-font-heading);
  font-size: 16px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-popup-window__actions {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.tool-popup-window__close {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: rgb(var(--toolbox-border-rgb) / 46%);
  color: var(--toolbox-text);
  cursor: pointer;
  transition: background 0.15s ease, transform 0.1s ease;
}

.tool-popup-window__close:hover,
.tool-popup-window__close:focus-visible {
  background: rgb(var(--toolbox-border-rgb) / 82%);
  transform: scale(1.05);
  outline: 0;
}

.tool-popup-window__body {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

/* Stretch the single body child (the ``data-tool-id`` wrapper from App.vue,
   or a direct panel like FontSizesPanel) so it hands a definite height down
   to the tool surface. Without this the wrapper is a height:auto block and
   the surface's min-height:100% resolves to its content, leaving a flat
   band of the shell panel colour when the window is enlarged. */
.tool-popup-window__body > :deep(*) {
  flex: 1 0 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  width: 100%;
}

/* Maximized: the shell takes over the WHOLE viewport — no padding, no
   rounded corners, nothing visible behind. The header (with the restore
   + close buttons) stays on top. */
.tool-popup-window--max {
  padding: 0;
}

.tool-popup-window--max .tool-popup-window__shell {
  width: 100%;
  min-width: 0;
  max-width: none;
  height: 100vh;
  height: 100dvh;
  min-height: 0;
  max-height: none;
  resize: none;
  border: 0;
  border-radius: 0;
}

@media (max-width: 640px), (max-height: 520px) {
  .tool-popup-window {
    padding: 12px;
  }

  .tool-popup-window__shell {
    min-height: min(280px, calc(100dvh - 24px));
    max-height: calc(100vh - 24px);
    max-height: calc(100dvh - 24px);
  }

  .tool-popup-window--max {
    padding: 0;
  }
}

/* The popped tools are full-page views (.tool-surface with a 100vh
   minimum). Inside the window that minimum would force useless
   scrolling, so relax it and tighten the padding. */
.tool-popup-window__body :deep(.tool-surface) {
  flex: 1 0 auto;
  width: 100%;
  min-height: 100%;
  padding: 18px;
}
</style>
