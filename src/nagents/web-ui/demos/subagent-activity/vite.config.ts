import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  publicDir: false,
  build: {
    outDir: fileURLToPath(new URL("../../../../../dist/subagent-concepts", import.meta.url)),
    emptyOutDir: true,
  },
  server: { host: "127.0.0.1", port: 4175, strictPort: true },
});
