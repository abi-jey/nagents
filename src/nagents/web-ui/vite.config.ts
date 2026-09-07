import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "../web/static",
    emptyOutDir: true,
    sourcemap: false,
  },
});
