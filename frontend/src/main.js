import { createApp } from "vue";

import { initTheme } from "./shared/theme";
import "./studio/i18n/studioMessages";
import App from "./App.vue";

initTheme();
createApp(App).mount("#app");
