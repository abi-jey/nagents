import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { assetInventory, buildFrontend, sourceInventory } from "./build.mjs";

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "ngn-ui-build-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const sourceRoot = path.join(root, "web-ui");
  const staticRoot = path.join(root, "web/static");
  async function put(relative, content = relative) {
    const target = path.join(root, relative);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, content);
  }
  await put("web-ui/package.json", "{}");
  await put("web-ui/src/app.ts", "source");
  await put("web/static/index.html", "built");
  return { root, sourceRoot, staticRoot, put };
}

test("source inventory includes root inputs and only supported trees", async (t) => {
  const { sourceRoot, put } = await fixture(t);
  for (const relative of [
    "public/logo.svg", "scripts/build.mjs", "src/deep/component.ts", "vite.config.ts",
    "node_modules/ignored.js", ".test-build/ignored.js", "other/ignored.js",
    "src/node_modules/ignored.js", "src/.git/ignored", "src/.cache/ignored",
    "src/.vite/ignored", "src/__pycache__/ignored", "src/.test-build/ignored",
    "tsconfig.tsbuildinfo", "src/nested.tsbuildinfo",
  ]) await put(`web-ui/${relative}`);
  const sources = await sourceInventory(sourceRoot);
  assert.deepEqual(Object.keys(sources), [
    "package.json", "public/logo.svg", "scripts/build.mjs", "src/app.ts",
    "src/deep/component.ts", "vite.config.ts",
  ]);
  assert.equal(sources["src/app.ts"], createHash("sha256").update("source").digest("hex"));
});

test("source inventory skips symbolic links", { skip: process.platform === "win32" }, async (t) => {
  const { sourceRoot } = await fixture(t);
  const before = await sourceInventory(sourceRoot);
  await symlink(path.join(sourceRoot, "src/app.ts"), path.join(sourceRoot, "linked.ts"));
  await symlink(path.join(sourceRoot, "src"), path.join(sourceRoot, "src/linked"));
  assert.deepEqual(await sourceInventory(sourceRoot), before);
});

test("successful build records deterministic source and asset hashes", async (t) => {
  const { sourceRoot, staticRoot, put } = await fixture(t);
  await put("web/static/build.json", "old");
  const build = async () => {
    await assert.rejects(readFile(path.join(staticRoot, "build.json")), { code: "ENOENT" });
    await put("web/static/assets/app.js", "output");
  };
  await buildFrontend(sourceRoot, build);
  const manifestPath = path.join(staticRoot, "build.json");
  const first = await readFile(manifestPath, "utf8");
  assert.deepEqual(JSON.parse(first), {
    version: 1,
    sources: await sourceInventory(sourceRoot),
    assets: await assetInventory(staticRoot),
  });
  assert.equal(JSON.parse(first).assets["assets/app.js"], createHash("sha256").update("output").digest("hex"));
  await buildFrontend(sourceRoot, build);
  assert.equal(await readFile(manifestPath, "utf8"), first);
  await put("web/static/assets/app.js", "corrupted");
  assert.notDeepEqual(await assetInventory(staticRoot), JSON.parse(first).assets);
});

for (const mutation of ["edit", "add", "delete"]) {
  test(`source ${mutation} during build leaves no manifest`, async (t) => {
    const { sourceRoot, staticRoot, put } = await fixture(t);
    await put("web/static/build.json", "old");
    await assert.rejects(buildFrontend(sourceRoot, async () => {
      if (mutation === "edit") await put("web-ui/src/app.ts", "changed");
      if (mutation === "add") await put("web-ui/src/new.ts", "added");
      if (mutation === "delete") await rm(path.join(sourceRoot, "src/app.ts"));
    }), /sources changed/);
    await assert.rejects(readFile(path.join(staticRoot, "build.json")), { code: "ENOENT" });
  });
}

test("build failure invalidates the previous manifest", async (t) => {
  const { sourceRoot, staticRoot, put } = await fixture(t);
  await put("web/static/build.json", "old");
  await assert.rejects(buildFrontend(sourceRoot, () => { throw new Error("typecheck failed"); }), /typecheck failed/);
  await assert.rejects(readFile(path.join(staticRoot, "build.json")), { code: "ENOENT" });
});

test("missing build output cannot receive a manifest", async (t) => {
  const { sourceRoot, staticRoot } = await fixture(t);
  await rm(path.join(staticRoot, "index.html"));
  await assert.rejects(buildFrontend(sourceRoot, () => {}), /did not produce index.html/);
  await assert.rejects(readFile(path.join(staticRoot, "build.json")), { code: "ENOENT" });
});
