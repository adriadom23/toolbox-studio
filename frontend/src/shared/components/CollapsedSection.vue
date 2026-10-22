<template>
  <section class="tb-fold" :class="{ 'tb-fold--open': open }">
    <!-- Collapsed bar: the whole container folds into this single row.
         Clicking toggles; dragging a file over it auto-expands so the
         drop can land on the dropzone inside. It's a div (not a
         button) so the actions slot can host real buttons. -->
    <div
      class="tb-fold__bar"
      role="button"
      tabindex="0"
      :aria-expanded="open ? 'true' : 'false'"
      @click="toggle"
      @keydown.enter.prevent="toggle"
      @keydown.space.prevent="toggle"
      @dragenter.prevent="open = true"
      @dragover.prevent="open = true"
    >
      <span v-if="badge" class="tb-fold__badge">{{ badge }}</span>
      <span class="tb-fold__label">{{ label }}</span>
      <span class="tb-fold__summary">
        <!-- Rich status content (e.g. connection chips) or plain text.
             Only rendered while collapsed: expanded sections already show
             the full status inside (IdentifyPanel chips, dropzone file
             names...), so repeating it on the bar duplicates it. The span
             itself stays (flex: 1) so label/actions/chevron keep their
             places. -->
        <template v-if="!open">
          <slot name="summary">{{ summary }}</slot>
        </template>
      </span>
      <!-- Right-aligned actions shown while collapsed (e.g. the extract
           button that lives inside the folded panel). Clicks must not
           toggle the section. With ``stickyActions`` they stay visible
           while expanded too (controls that must never disappear, e.g.
           the Flow NS "refrescar todo" + auto-refresh). -->
      <span
        v-if="$slots.actions && (stickyActions || !open)"
        class="tb-fold__actions"
        @click.stop
        @keydown.stop
      >
        <!-- ``open`` is exposed so sticky consumers can hide the buttons
             that would duplicate controls inside the expanded body. -->
        <slot name="actions" :open="open" />
      </span>
      <span class="tb-fold__chevron" aria-hidden="true">{{ open ? "▾" : "▸" }}</span>
    </div>
    <div v-show="open" class="tb-fold__body">
      <slot />
    </div>
  </section>
</template>

<script setup>
import { ref, watch } from "vue";

const props = defineProps({
  /** Bar text — typically the step title ("Cargar CSV"). */
  label: { type: String, required: true },
  /** Optional step badge number shown before the label. */
  badge: { type: [String, Number], default: "" },
  /** Optional status text on the bar while collapsed (e.g. file name). */
  summary: { type: String, default: "" },
  /** Render expanded on first mount. */
  startOpen: { type: Boolean, default: false },
  /**
   * While true the section is forced open (e.g. a file is loaded, an
   * automated source is filling the slot, or an error must be seen).
   */
  forceOpen: { type: Boolean, default: false },
  /**
   * What happens when ``forceOpen`` turns off. By default the section
   * folds itself away (extraction panels: the results live elsewhere).
   * Set to false when the results live INSIDE the section (e.g. the
   * Scorecard Exceptions analyses): it stays open and the user can
   * fold it manually.
   */
  foldOnRelease: { type: Boolean, default: true },
  /**
   * Keep the ``actions`` slot visible while the section is expanded too
   * (by default it only shows on the collapsed bar). For controls that
   * must never disappear regardless of the fold state.
   */
  stickyActions: { type: Boolean, default: false },
});

const open = ref(props.startOpen || props.forceOpen);

// Track forceOpen in both directions: while it's on the section must be
// visible (disconnected session, loaded file...), and when it turns off
// (e.g. the source just connected) the section folds itself away again —
// unless ``foldOnRelease`` is off, in which case it stays as-is.
watch(
  () => props.forceOpen,
  (val) => {
    if (val) {
      open.value = true;
    } else if (props.foldOnRelease) {
      open.value = props.startOpen;
    }
  },
);

function toggle() {
  if (props.forceOpen) return;
  open.value = !open.value;
}
</script>

<style scoped>
.tb-fold {
  border: 1px solid rgb(var(--toolbox-border-rgb, 31 111 139) / 45%);
  border-radius: 8px;
  background: rgb(var(--toolbox-surface-rgb, 255 255 255) / 20%);
}

.tb-fold__bar {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 12px;
  border: 0;
  background: none;
  color: var(--toolbox-text);
  font-family: inherit;
  cursor: pointer;
  text-align: left;
  box-sizing: border-box;
}

.tb-fold__bar:focus-visible {
  outline: 2px solid var(--toolbox-accent);
  outline-offset: -2px;
  border-radius: 8px;
}

.tb-fold__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--toolbox-accent);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
}

.tb-fold__label {
  font-size: 13.5px;
  font-weight: 700;
  white-space: nowrap;
}

.tb-fold__summary {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--toolbox-muted);
  font-size: 11.5px;
  text-align: right;
  white-space: nowrap;
}

.tb-fold__actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.tb-fold__chevron {
  flex: none;
  color: var(--toolbox-muted);
  font-size: 12px;
}

.tb-fold__body {
  display: grid;
  gap: 12px;
  padding: 0 12px 12px;
}
</style>
