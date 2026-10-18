import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  cacheDir: fileURLToPath(new URL("../../node_modules/.vite/chat-flow", import.meta.url)),
  base: "./", publicDir: false,
  build: { outDir: fileURLToPath(new URL("../../../../../dist/chat-flow", import.meta.url)), emptyOutDir: true },
  server: { host: "127.0.0.1", port: 18892, strictPort: true },
});
