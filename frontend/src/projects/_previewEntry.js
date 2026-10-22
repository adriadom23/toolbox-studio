// "Open in dev server" mount point (design.md §5.4).
//
// ProjectEditor.vue's "Open in dev server" button opens
// http://127.0.0.1:5173/projects/<name>/preview in a new tab. This module
// is what actually renders at that URL: it reads <name> from the path,
// dynamically imports that project's scaffolded .vue file, and mounts it
// standalone — WITHOUT Studio's own shell chrome (no StudioShell top bar,
// no ProjectList/ProjectEditor around it) — on Vite's own HMR-enabled dev
// server, with the vendored theme CSS applied and initTheme() called so the
// component looks exactly like it will once copied into ToolBox.
//
// Route-serving note (checked during implementation, per this step's own
// instruction to verify rather than assume): Vite's dev server defaults to
// appType: "spa" (vite.config.js does not override it), whose built-in
// history-API fallback serves the single frontend/index.html for ANY
// request path that doesn't look like a static file request (no
// extension) — see https://vitejs.dev/config/shared-options.html#apptype.
// That means /projects/<name>/preview already resolves to index.html (and
// therefore to src/main.js) with NO explicit vite.config.js server rule
// needed. main.js inspects location.pathname before mounting the Studio
// shell and, for a /projects/<name>/preview URL, delegates to
// mountProjectPreview() below instead. If a project name ever needs a
// literal "." in it this would stop matching Vite's fallback — the slug
// validation in app/projects/scaffold.py (design.md §6.2) already forbids
// that character, so this is not a real-world gap.
import { createApp, defineAsyncComponent, h } from "vue";

import "../shared/styles/toolbox-theme.css";
import "../shared/styles/toolbox-controls.css";
import "../shared/styles/toolbox-fonts.css";
import { initTheme } from "../shared/theme";

const PREVIEW_PATH_RE = /^\/projects\/([^/]+)\/preview\/?$/;

/** Match the current location against the preview route; returns the
 * project name (slug) when it matches, or null otherwise. */
export function matchPreviewPath(pathname) {
  const match = PREVIEW_PATH_RE.exec(pathname || "");
  return match ? decodeURIComponent(match[1]) : null;
}

/** Guess the scaffolded component's PascalCase filename from its slug,
 * mirroring scaffold.py's own derivation (design.md §6.2): slug segments
 * title-cased and joined, e.g. "dock-health-monitor" -> "DockHealthMonitor". */
function pascalNameFromSlug(slug) {
  return slug
    .split("-")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("");
}

/** Mount the given project's scaffolded .vue file standalone at #app,
 * with the vendored theme applied but no Studio shell chrome around it. */
export function mountProjectPreview(name, rootSelector = "#app") {
  initTheme();
  const toolName = pascalNameFromSlug(name);

  const PreviewRoot = defineAsyncComponent({
    loader: () => import(`./${name}/${toolName}.vue`),
    onError(error, retry, fail) {
      console.error(`[preview] failed to load ./${name}/${toolName}.vue`, error);
      fail();
    },
  });

  const app = createApp({
    render() {
      return h("div", { class: "studio-preview-root" }, [h(PreviewRoot)]);
    },
  });
  app.mount(rootSelector);
  return app;
}
