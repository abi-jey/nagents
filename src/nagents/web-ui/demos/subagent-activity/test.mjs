import { createServer } from "vite";
import { fileURLToPath } from "node:url";

const server = await createServer({
  root: fileURLToPath(new URL(".", import.meta.url)),
  configFile: false,
  server: { middlewareMode: true },
  appType: "custom",
});
try {
  await server.ssrLoadModule("/src/engine.test.ts");
  await server.ssrLoadModule("/src/agentRuntime.test.ts");
  await server.ssrLoadModule("/src/teamPresentation.test.ts");
  await server.ssrLoadModule("/src/mainRuntime.test.ts");
  await server.ssrLoadModule("/src/agentLifecycle.test.ts");
  await server.ssrLoadModule("/src/activityTree.test.ts");
} finally {
  await server.close();
}
