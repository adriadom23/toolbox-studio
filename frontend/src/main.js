// Toolbox Studio shell entrypoint (design.md §5.1/§5.2).
//
// Mirrors ToolBox's own main.js at Studio's much smaller scale: apply the
// persisted theme BEFORE mounting (so there's no flash of the wrong theme),
// then mount the router-less App.vue.
//
// One exception to "always mount the Studio shell": the "Open in dev
// server" preview route (design.md §5.4) is served by this SAME
// index.html/main.js (Vite's default SPA history fallback — see
// projects/_previewEntry.js's header comment for the full reasoning), so
// this entry checks the URL first and delegates to the dedicated preview
// mount point instead of the Studio shell when it matches.
import { createApp } from "vue";

import { initTheme } from "./shared/theme";
// Side-effect import: merges Studio's own `studio.*` strings into the shared
// i18n catalog BEFORE App.vue (and its studio components) render, so every
// t("studio.…") call resolves. See studioMessages.js's header comment.
import "./studio/i18n/studioMessages";
import { matchPreviewPath, mountProjectPreview } from "./projects/_previewEntry";
import App from "./App.vue";

const previewProject = matchPreviewPath(window.location.pathname);

if (previewProject) {
  mountProjectPreview(previewProject);
} else {
  initTheme();
  createApp(App).mount("#app");
}
