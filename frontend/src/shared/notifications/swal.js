import Swal from "sweetalert2";

const TOOLBOX_SWAL_CLASSES = Object.freeze({
  container: "toolbox-swal-container",
  popup: "toolbox-swal-popup",
  title: "toolbox-swal-title",
  htmlContainer: "toolbox-swal-content",
  actions: "toolbox-swal-actions",
  confirmButton: "toolbox-swal-button toolbox-swal-button--confirm",
  cancelButton: "toolbox-swal-button toolbox-swal-button--cancel",
});

export const toolboxSwal = Swal.mixin({
  customClass: TOOLBOX_SWAL_CLASSES,
  buttonsStyling: false,
  allowEscapeKey: true,
  returnFocus: true,
  reverseButtons: true,
  showClass: {
    popup: "toolbox-swal-enter",
  },
  hideClass: {
    popup: "toolbox-swal-leave",
  },
});

export function showToolboxError({ title, text, confirmButtonText }) {
  return toolboxSwal.fire({
    icon: "error",
    title,
    text,
    confirmButtonText,
  });
}

export function showToolboxWarning({
  title,
  text,
  html,
  confirmButtonText,
  cancelButtonText,
  showCancelButton = false,
}) {
  return toolboxSwal.fire({
    icon: "warning",
    title,
    // ``html`` wins over ``text`` when provided (caller is responsible
    // for escaping any dynamic values it interpolates).
    ...(html ? { html } : { text }),
    confirmButtonText,
    cancelButtonText,
    showCancelButton,
    // Confirmations are fail-closed: Enter immediately after opening must
    // choose the safe cancellation path, not execute a destructive action.
    focusCancel: showCancelButton,
  });
}

// Cross-tool dependency warning with navigation: "go to the missing tool"
// (a select picker when several are missing) + a dismiss button. Resolves
// to the chosen tool id, or "" when dismissed.
export async function showToolboxMissingToolsPrompt({
  title,
  text,
  tools = [], // [{ id, name }]
  goButtonText,
  dismissButtonText,
}) {
  if (!tools.length) return "";
  const config = {
    icon: "warning",
    title,
    text,
    showCancelButton: true,
    focusCancel: true,
    confirmButtonText: goButtonText,
    cancelButtonText: dismissButtonText,
    // The mixin renders cancel first (reverseButtons: true); here the
    // navigation action must always come first, with dismiss after it.
    // ``--forward`` keeps that order when the buttons stack on narrow /
    // split views (the theme's default stack is column-reverse).
    reverseButtons: false,
    customClass: {
      ...TOOLBOX_SWAL_CLASSES,
      actions: "toolbox-swal-actions toolbox-swal-actions--forward",
      input: "toolbox-swal-select",
    },
  };
  if (tools.length > 1) {
    config.input = "select";
    config.inputOptions = Object.fromEntries(tools.map((tool) => [tool.id, tool.name]));
    config.inputValue = tools[0].id;
  }
  const result = await toolboxSwal.fire(config);
  if (!result.isConfirmed) return "";
  return tools.length > 1 ? String(result.value || tools[0].id) : tools[0].id;
}

// Themed replacement for ``window.prompt``: resolves to the trimmed input
// when confirmed, or ``""`` when cancelled/dismissed (fail-closed, same
// contract as a cancelled native prompt).
export async function showToolboxPrompt({
  title,
  text,
  inputPlaceholder,
  inputValue = "",
  confirmButtonText,
  cancelButtonText,
}) {
  const result = await toolboxSwal.fire({
    icon: "question",
    title,
    text,
    input: "text",
    inputPlaceholder,
    inputValue,
    showCancelButton: true,
    focusCancel: true,
    confirmButtonText,
    cancelButtonText,
  });
  if (!result.isConfirmed) return "";
  return String(result.value || "").trim();
}

// Themed multi-number prompt. Dynamic labels/values are assigned through DOM
// properties in didOpen (never interpolated as HTML). Returns a key->number
// mapping on confirm, or null when cancelled.
export async function showToolboxNumberFieldsPrompt({
  title,
  text,
  fields = [], // [{ key, label, value?, min?, max?, step? }]
  confirmButtonText,
  cancelButtonText,
  invalidText,
}) {
  const normalized = fields.filter((field) => field?.key);
  if (!normalized.length) return {};

  const result = await toolboxSwal.fire({
    icon: "warning",
    title,
    html:
      '<p class="toolbox-swal-number-intro" data-toolbox-number-intro></p>' +
      '<div class="toolbox-swal-number-grid" data-toolbox-number-grid></div>',
    showCancelButton: true,
    focusCancel: true,
    confirmButtonText,
    cancelButtonText,
    didOpen: (popup) => {
      const intro = popup.querySelector("[data-toolbox-number-intro]");
      const grid = popup.querySelector("[data-toolbox-number-grid]");
      if (intro) intro.textContent = String(text || "");
      if (!grid) return;
      normalized.forEach((field, index) => {
        const label = document.createElement("label");
        label.className = "toolbox-swal-number-field";
        const caption = document.createElement("span");
        caption.textContent = String(field.label || field.key);
        const input = document.createElement("input");
        input.className = "toolbox-swal-number-input";
        input.type = "number";
        input.inputMode = "numeric";
        input.min = String(field.min ?? 1);
        input.step = String(field.step ?? 1);
        if (field.max != null) input.max = String(field.max);
        if (field.value != null && field.value !== "") input.value = String(field.value);
        input.dataset.toolboxNumberIndex = String(index);
        input.setAttribute("aria-label", caption.textContent);
        label.append(caption, input);
        grid.appendChild(label);
      });
      grid.querySelector("input")?.focus();
    },
    preConfirm: () => {
      const popup = Swal.getPopup();
      const values = {};
      for (let index = 0; index < normalized.length; index += 1) {
        const input = popup?.querySelector(`[data-toolbox-number-index="${index}"]`);
        const value = input?.valueAsNumber;
        const field = normalized[index];
        const min = Number(field.min ?? 1);
        const max = field.max == null ? Number.MAX_SAFE_INTEGER : Number(field.max);
        if (!Number.isSafeInteger(value) || value < min || value > max) {
          Swal.showValidationMessage(String(invalidText || ""));
          input?.focus();
          return false;
        }
        values[field.key] = value;
      }
      return values;
    },
  });
  return result.isConfirmed ? result.value || {} : null;
}