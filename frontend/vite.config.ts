import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/pos/",
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8790",
      "/health": "http://127.0.0.1:8790",
    },
  },
  build: {
    outDir: "../eluno_pos/web/react-build",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    globals: true,
    css: false,
  },
});
