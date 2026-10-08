import { readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const root = fileURLToPath(new URL(".", import.meta.url));
const server = await createServer({
  root,
  configFile: false,
  server: { middlewareMode: true },
  appType: "custom",
  esbuild: { jsx: "automatic" },
});
try {
  for (const name of (await readdir(new URL("./src/", import.meta.url))).filter(name => /\.test\.tsx?$/.test(name)).sort()) {
    await server.ssrLoadModule(`/src/${name}`);
  }
} finally {
  await server.close();
}
