<template>
  <teleport to="body">
    <transition name="tb-modal" @after-leave="onAfterLeave">
      <div
        v-if="modelValue"
        class="tb-modal"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        @click.self="close"
        @keydown.esc.stop="closeTop"
      >
        <div ref="shell" class="tb-modal__shell" tabindex="-1" :style="sizeStyle">
          <header class="tb-modal__header">
            <div>
              <p v-if="eyebrow" class="tb-modal__eyebrow">{{ eyebrow }}</p>
              <h3 :id="titleId" class="tb-modal__title">{{ title }}</h3>
            </div>
            <button
              type="button"
              class="tb-modal__close"
              :aria-label="t('components.modal.close')"
              @click="close"
            >
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path
                  d="M5.3 4 4 5.3 8.7 10 4 14.7 5.3 16 10 11.3 14.7 16 16 14.7 11.3 10 16 5.3 14.7 4 10 8.7 5.3 4z"
                  fill="currentColor"
                />
              </svg>
            </button>
          </header>

          <div class="tb-modal__body">
            <slot />
          </div>

          <footer v-if="$slots.footer" class="tb-modal__footer">
            <slot name="footer" />
          </footer>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from "vue";

import { useI18n } from "../i18n";

const { t } = useI18n();
const titleId = `tb-modal-title-${useId()}`;

const props = defineProps({
  modelValue: {
    type: Boolean,
    required: true,
  },
  title: {
    type: String,
    required: true,
  },
  eyebrow: {
    type: String,
    default: "",
  },
  maxWidth: {
    type: [String, Number],
    default: 640,
  },
});

const emit = defineEmits(["update:modelValue", "close"]);

const sizeStyle = computed(() => ({
  "max-width": typeof props.maxWidth === "number" ? `${props.maxWidth}px` : String(props.maxWidth),
}));
const shell = ref(null);
let previousActiveElement = null;
let registeredOpen = false;
let restoreAfterLeave = false;
let leaving = false;

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusableElements() {
  if (!shell.value) return [];
  return [...shell.value.querySelectorAll(FOCUSABLE_SELECTOR)].filter((element) => {
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

function isTopModal() {
  if (!shell.value) return false;
  const modals = [...document.querySelectorAll('.tb-modal')].filter(
    (modal) => modal.getClientRects().length > 0,
  );
  return modals.at(-1) === shell.value.closest('.tb-modal');
}

function captureFocusReturnTarget() {
  const active = document.activeElement;
  const ownerShell = active?.closest?.('.tb-modal__shell');
  const inherited = ownerShell?.dataset.tbModalLeaving === 'true'
    ? ownerShell.__tbFocusReturn
    : null;
  return inherited?.isConnected ? inherited : active;
}

function registerOpenModal() {
  if (registeredOpen) return;
  registeredOpen = true;
  const current = Number(document.body.dataset.tbModalCount || 0);
  const next = current + 1;
  document.body.dataset.tbModalCount = String(next);
  document.body.classList.toggle("tb-modal-open", next > 0);
}

function unregisterOpenModal() {
  if (!registeredOpen) return;
  registeredOpen = false;
  const current = Number(document.body.dataset.tbModalCount || 0);
  const next = Math.max(0, current - 1);
  if (next) document.body.dataset.tbModalCount = String(next);
  else delete document.body.dataset.tbModalCount;
  document.body.classList.toggle("tb-modal-open", next > 0);
}

function onAfterLeave() {
  if (props.modelValue) return;
  leaving = false;
  window.removeEventListener("keydown", onKeydown, true);
  unregisterOpenModal();

  const visibleModals = [...document.querySelectorAll('.tb-modal')].filter(
    (modal) => modal.getClientRects().length > 0,
  );
  const activeModal = visibleModals.at(-1) || null;
  const focusTargetBelongsToActiveModal =
    !activeModal || activeModal.contains(previousActiveElement);
  if (
    restoreAfterLeave &&
    focusTargetBelongsToActiveModal &&
    previousActiveElement?.isConnected
  ) {
    previousActiveElement.focus({ preventScroll: true });
  }
  previousActiveElement = null;
  restoreAfterLeave = false;
  if (shell.value) {
    delete shell.value.__tbFocusReturn;
    delete shell.value.dataset.tbModalLeaving;
  }
}

function close() {
  emit("update:modelValue", false);
  emit("close");
}

function closeTop() {
  if (props.modelValue && isTopModal()) close();
}

function onKeydown(event) {
  if ((!props.modelValue && !leaving) || !isTopModal()) return;

  // The shell remains visible during its leave transition. Keep keyboard
  // ownership until after-leave so no lower popup/global handler sees keys.
  if (leaving) {
    if (event.key === "Escape" || event.key === "Tab") {
      event.preventDefault();
      event.stopImmediatePropagation();
      shell.value?.focus({ preventScroll: true });
    }
    return;
  }

  if (event.key === "Escape") {
    event.preventDefault();
    event.stopImmediatePropagation();
    close();
    return;
  }
  if (event.key !== "Tab") return;

  const focusable = focusableElements();
  if (!focusable.length) {
    event.preventDefault();
    shell.value?.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !shell.value?.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !shell.value?.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (typeof document === "undefined") return;
    if (open) {
      leaving = false;
      restoreAfterLeave = false;
      if (shell.value) shell.value.dataset.tbModalLeaving = "false";
      const shouldCaptureOpener = !registeredOpen;
      registerOpenModal();
      window.addEventListener("keydown", onKeydown, true);
      nextTick(() => {
        // Defer capture until all sibling watchers have run. If this modal
        // replaces one entering leave in the same render, its leaving marker
        // and external return target are now available regardless of order.
        if (shouldCaptureOpener) previousActiveElement = captureFocusReturnTarget();
        if (shell.value) {
          shell.value.dataset.tbModalLeaving = "false";
          shell.value.__tbFocusReturn = previousActiveElement;
        }
        const first = focusableElements()[0];
        (first || shell.value)?.focus({ preventScroll: true });
      });
    } else {
      // Keep tb-modal-open and the keyboard listener registered until the
      // leave transition is gone; the popup underneath must not regain
      // Escape/Tab while this shell is still visible.
      leaving = registeredOpen;
      if (shell.value) shell.value.dataset.tbModalLeaving = String(leaving);
      restoreAfterLeave = isTopModal();
    }
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  const shouldRestoreFocus = isTopModal();
  unregisterOpenModal();
  window.removeEventListener("keydown", onKeydown, true);
  if (shouldRestoreFocus && previousActiveElement?.isConnected) {
    previousActiveElement.focus({ preventScroll: true });
  }
  if (shell.value) {
    delete shell.value.__tbFocusReturn;
    delete shell.value.dataset.tbModalLeaving;
  }
});
</script>

<style scoped>
.tb-modal {
  position: fixed;
  inset: 0;
  z-index: 10000;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgb(12 18 24 / 64%);
  backdrop-filter: blur(6px) saturate(130%);
  -webkit-backdrop-filter: blur(6px) saturate(130%);
}

.tb-modal__shell {
  width: 100%;
  max-height: 90vh;
  display: grid;
  grid-template-rows: auto 1fr auto;
  overflow: hidden;
  border: 1px solid rgb(var(--toolbox-border-rgb) / 60%);
  border-radius: 14px;
  background: rgb(var(--toolbox-panel-rgb) / 96%);
  color: var(--toolbox-text);
  box-shadow: 0 30px 80px rgb(12 18 24 / 38%);
}

.tb-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 22px;
  border-bottom: 1px solid rgb(var(--toolbox-border-rgb) / 56%);
}

.tb-modal__eyebrow {
  margin: 0;
  color: var(--toolbox-muted);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.tb-modal__title {
  margin: 2px 0 0;
  font-family: var(--tb-font-heading);
  font-size: 20px;
}

.tb-modal__close {
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

.tb-modal__close:hover,
.tb-modal__close:focus-visible {
  background: rgb(var(--toolbox-border-rgb) / 82%);
  transform: scale(1.05);
  outline: 0;
}

.tb-modal__close svg {
  width: 16px;
  height: 16px;
}

.tb-modal__body {
  padding: 20px 22px;
  overflow: auto;
}

.tb-modal__footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 22px;
  border-top: 1px solid rgb(var(--toolbox-border-rgb) / 56%);
  background: rgb(var(--toolbox-border-rgb) / 22%);
}

.tb-modal-enter-active,
.tb-modal-leave-active {
  transition: opacity 0.18s ease;
}

.tb-modal-enter-active .tb-modal__shell,
.tb-modal-leave-active .tb-modal__shell {
  transition: transform 0.22s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.18s ease;
}

.tb-modal-enter-from,
.tb-modal-leave-to {
  opacity: 0;
}

.tb-modal-enter-from .tb-modal__shell,
.tb-modal-leave-to .tb-modal__shell {
  transform: translateY(10px) scale(0.98);
  opacity: 0;
}
</style>

<style>
body.tb-modal-open {
  overflow: hidden;
}
</style>
