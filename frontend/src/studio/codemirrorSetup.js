// CodeMirror 6 editor factory for ProjectEditor.vue (design.md §5.3).
//
// Uses ONLY the officially-published packages pinned in package.json:
//   @codemirror/view, @codemirror/state, @codemirror/language,
//   @codemirror/lang-html, @codemirror/lang-python, @codemirror/lang-javascript
// (@codemirror/language is a transitive dep of the lang-* packages and
// provides syntaxHighlighting/defaultHighlightStyle/indentOnInput/
// bracketMatching). There is NO official @codemirror/lang-vue — .vue files
// are highlighted with lang-html per the design's corrected decision (§5.3).
//
// Deliberately does NOT depend on @codemirror/commands (not in package.json):
// the editor keeps CM6's built-in editing; Ctrl+S / Cmd+S is bound by the
// editor's OWN keymap here (so the browser's "save page" is suppressed) and
// routed to the Vue component via the onSave callback.
//
// The whole module is imported dynamically by ProjectEditor.vue inside a
// try/catch, so if the CM bundle ever fails to load the editor falls back to
// a plain <textarea> (feature detection, §5.3) without breaking the workspace.
import { EditorState } from "@codemirror/state";
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  drawSelection,
  rectangularSelection,
} from "@codemirror/view";
import {
  syntaxHighlighting,
  defaultHighlightStyle,
  bracketMatching,
  indentOnInput,
  foldGutter,
} from "@codemirror/language";
import { html } from "@codemirror/lang-html";
import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";

/** Pick the language extension for a file path by extension (design.md §5.3):
 * .vue → HTML mode, .py → Python mode, .js/.mjs/.cjs/.json → JS mode,
 * anything else → no language (plain text with the base editor chrome). */
function languageFor(path) {
  const lower = String(path || "").toLowerCase();
  if (lower.endsWith(".vue") || lower.endsWith(".html") || lower.endsWith(".htm")) {
    return html();
  }
  if (lower.endsWith(".py")) return python();
  if (
    lower.endsWith(".js") ||
    lower.endsWith(".mjs") ||
    lower.endsWith(".cjs") ||
    lower.endsWith(".json")
  ) {
    return javascript();
  }
  return null;
}

/**
 * Create a CodeMirror editor mounted in ``parent``.
 *
 * @param {object} opts
 * @param {HTMLElement} opts.parent    container element
 * @param {string}      opts.doc       initial document text
 * @param {string}      opts.path      file path (drives the language mode)
 * @param {Function}    opts.onChange  (text) => void, called on every edit
 * @param {Function}    opts.onSave    () => void, called on Ctrl+S / Cmd+S
 * @returns an object with { view, getValue() } — call view.destroy() to tear down.
 */
export function createEditor({ parent, doc, path, onChange, onSave }) {
  const language = languageFor(path);

  const saveKeymap = keymap.of([
    {
      key: "Mod-s",
      preventDefault: true,
      run() {
        if (typeof onSave === "function") onSave();
        return true;
      },
    },
  ]);

  const updateListener = EditorView.updateListener.of((update) => {
    if (update.docChanged && typeof onChange === "function") {
      onChange(update.state.doc.toString());
    }
  });

  const extensions = [
    lineNumbers(),
    highlightActiveLineGutter(),
    foldGutter(),
    drawSelection(),
    rectangularSelection(),
    highlightActiveLine(),
    bracketMatching(),
    indentOnInput(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    EditorView.lineWrapping,
    saveKeymap,
    updateListener,
  ];
  if (language) extensions.push(language);

  const view = new EditorView({
    state: EditorState.create({ doc: doc ?? "", extensions }),
    parent,
  });

  return {
    view,
    getValue: () => view.state.doc.toString(),
  };
}
