import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Dev workflow: ``npm run dev`` serves the SPA on 5173 and the
// browser hits ``/api/*``. The proxy below forwards those calls to
// the Flask backend on 127.0.0.1:5000 so the same relative URL works
// in dev and in any later single-port deployment.
//
// Override with ``VITE_API_PROXY_TARGET=http://127.0.0.1:5001`` when
// the backend runs on a non-default port locally.
const API_PROXY_TARGET = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:5000";

export default defineConfig({
  plugins: [vue()],
  server: {
    watch: {
      // ``frontend/dist`` is build output only; nothing in dev needs to
      // watch it, and watching it wastes file-watcher handles.
      ignored: ["**/dist/**"],
    },
    proxy: {
      "/api": {
        target: API_PROXY_TARGET,
        changeOrigin: true,
        secure: false,
      },
    },
  },
});
