import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  cacheDir: fileURLToPath(new URL("../../node_modules/.vite/panel-dividers", import.meta.url)),
  publicDir: fileURLToPath(new URL("../../public", import.meta.url)),
  base: "./",
  server: { host: "127.0.0.1", port: 18896, strictPort: true },
  build: {
    outDir: fileURLToPath(new URL("../../../../../dist/panel-dividers", import.meta.url)),
    emptyOutDir: true,
  },
});
