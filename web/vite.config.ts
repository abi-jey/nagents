import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "../src/nagents/web/static",
    emptyOutDir: true,
    sourcemap: false,
  },
});
