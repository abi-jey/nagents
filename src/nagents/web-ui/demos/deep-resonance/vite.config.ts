import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  publicDir: false,
  build: {
    outDir: fileURLToPath(new URL("../../../../../dist/voice-concepts/react-v4", import.meta.url)),
    emptyOutDir: true,
    assetsInlineLimit: 0,
  },
  server: { host: "127.0.0.1", port: 4174, strictPort: true },
});
