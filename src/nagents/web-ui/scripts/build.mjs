import { createHash } from "node:crypto";
import { readdir, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const excludedDirectories = new Set([
  "node_modules", ".git", ".test-build", ".vite", ".cache", "__pycache__",
]);
const sourceDirectories = new Set(["src", "public", "scripts"]);

async function inventory(root, sources) {
  const files = [];
  async function visit(directory, relative = "") {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const name = relative ? `${relative}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (sources && (excludedDirectories.has(entry.name)
          || (!relative && !sourceDirectories.has(entry.name)))) continue;
        await visit(path.join(directory, entry.name), name);
      } else if (entry.isFile()) {
        if (sources ? entry.name.endsWith(".tsbuildinfo") : name === "build.json") continue;
        files.push(name);
      }
    }
  }
  await visit(root);
  const hashes = [];
  for (const file of files.sort()) {
    hashes.push([file, createHash("sha256").update(await readFile(path.join(root, file))).digest("hex")]);
  }
  return Object.fromEntries(hashes);
}

export const sourceInventory = (root) => inventory(root, true);
export const assetInventory = (root) => inventory(root, false);

function runBuild(sourceRoot) {
  const require = createRequire(path.join(sourceRoot, "package.json"));
  const commands = [
    [require.resolve("typescript/bin/tsc"), "--noEmit"],
    [path.join(path.dirname(require.resolve("vite/package.json")), "bin/vite.js"), "build"],
  ];
  for (const command of commands) {
    const result = spawnSync(process.execPath, command, { cwd: sourceRoot, stdio: "inherit" });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`Frontend build failed (${result.signal ?? result.status}).`);
  }
}

export async function buildFrontend(sourceRoot, build = runBuild) {
  const staticRoot = path.resolve(sourceRoot, "../web/static");
  const manifestPath = path.join(staticRoot, "build.json");
  // A failed or interrupted build must never leave a trusted old manifest.
  await rm(manifestPath, { force: true });
  const sources = await sourceInventory(sourceRoot);
  await build(sourceRoot);
  const assets = await assetInventory(staticRoot);
  if (!Object.hasOwn(assets, "index.html")) throw new Error("Frontend build did not produce index.html.");
  if (JSON.stringify(sources) !== JSON.stringify(await sourceInventory(sourceRoot))) {
    throw new Error("Frontend sources changed during the build; run npm run build again.");
  }
  await writeFile(manifestPath, `${JSON.stringify({ version: 1, sources, assets }, null, 2)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const sourceRoot = fileURLToPath(new URL("..", import.meta.url));
  buildFrontend(sourceRoot).catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
