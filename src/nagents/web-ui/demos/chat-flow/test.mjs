import { createServer } from "vite";
import { fileURLToPath } from "node:url";

const server = await createServer({ root: fileURLToPath(new URL(".", import.meta.url)), cacheDir: fileURLToPath(new URL("../../node_modules/.vite/chat-flow-test", import.meta.url)), configFile: false, server: { middlewareMode: true }, appType: "custom" });
try { await server.ssrLoadModule("/src/engine.test.ts"); }
finally { await server.close(); }
