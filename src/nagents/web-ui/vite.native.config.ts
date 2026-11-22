import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  envDir: false,
  build: {
    assetsInlineLimit: 0,
    outDir: "../../../ngn/dist/web-ui",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: fileURLToPath(new URL("index.native.html", import.meta.url)),
      output: {
        // Distinguish native chunks from the Python app's public assets.
        entryFileNames: "assets/native-[hash].js",
        chunkFileNames: "assets/native-[hash].js",
        assetFileNames: "assets/native-[hash][extname]",
      },
    },
  },
});
