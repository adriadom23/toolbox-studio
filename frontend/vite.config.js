import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const HOST = "127.0.0.1";
const API_PROXY_TARGET = process.env.VITE_API_PROXY_TARGET || `http://${HOST}:5000`;

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: "jsdom",
    globals: true,
    restoreMocks: true,
  },
  server: {
    host: HOST,
    port: 5173,
    strictPort: true,
    watch: {
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
