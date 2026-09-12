import { defineConfig } from "vite";

export default defineConfig({
  build: {
    // AudioWorklet modules must be self-hosted assets under script-src 'self'.
    assetsInlineLimit: 0,
    outDir: "../web/static",
    emptyOutDir: true,
    sourcemap: false,
  },
});
