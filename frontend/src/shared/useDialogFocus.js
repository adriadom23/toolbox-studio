import { nextTick, onBeforeUnmount, ref, watch } from "vue";

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * Keyboard/focus contract for custom modal shells that cannot use Modal.vue.
 * The returned ref belongs on the element carrying role="dialog".
 */
export function useDialogFocus(openRef, close) {
  const dialogRef = ref(null);
  let previousActive = null;

  function focusable() {
    if (!dialogRef.value) return [];
    return [...dialogRef.value.querySelectorAll(FOCUSABLE)].filter(
      (element) => element.getClientRects().length > 0,
    );
  }

  function isTopDialog() {
    const visible = [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')]
      .filter((element) => element.getClientRects().length > 0);
    return visible.at(-1) === dialogRef.value;
  }

  function onKeydown(event) {
    if (!openRef.value || !dialogRef.value || !isTopDialog()) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopImmediatePropagation();
      close?.();
      return;
    }
    if (event.key !== "Tab") return;
    const items = focusable();
    if (!items.length) {
      event.preventDefault();
      dialogRef.value.focus({ preventScroll: true });
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialogRef.value.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !dialogRef.value.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  }

  function release({ restore = true } = {}) {
    window.removeEventListener("keydown", onKeydown, true);
    if (restore && previousActive?.isConnected) previousActive.focus({ preventScroll: true });
    previousActive = null;
  }

  watch(openRef, async (open) => {
    if (open) {
      previousActive = document.activeElement;
      window.addEventListener("keydown", onKeydown, true);
      await nextTick();
      (focusable()[0] || dialogRef.value)?.focus({ preventScroll: true });
    } else {
      release();
    }
  });

  onBeforeUnmount(() => release({ restore: false }));
  return { dialogRef };
}
