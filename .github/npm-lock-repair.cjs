const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const { readFileSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

async function main() {
  const [root, npmHome] = process.argv.slice(2);
  assert(root && npmHome, "Pass the repository root and installed npm directory.");
  const pacote = require(require.resolve("pacote", { paths: [npmHome] }));
  const ssri = require(require.resolve("ssri", { paths: [npmHome] }));
  const path = join(root, "app", "web", "package-lock.json");
  const original = JSON.parse(execFileSync(
    "git", ["show", "HEAD:app/web/package-lock.json"], { cwd: root, encoding: "utf8" },
  ));
  assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), original);
  const repaired = structuredClone(original);
  const entries = Object.entries(repaired.packages).filter(([, entry]) =>
    entry.resolved && new URL(entry.resolved).hostname !== "registry.npmjs.org"
  );
  const options = {
    registry: "https://registry.npmjs.org",
    "fetch-retries": 0,
    "fetch-timeout": 15000,
    ignoreScripts: true,
    fullMetadata: true,
  };
  let next = 0;
  const worker = async () => {
    while (next < entries.length) {
      const [location, entry] = entries[next++];
      const name = location.split("node_modules/").at(-1);
      const spec = `${name}@${entry.version}`;
      const manifest = await pacote.manifest(spec, options);
      assert.equal(manifest.name, name);
      assert.equal(manifest.version, entry.version);
      const { tarball, integrity, shasum } = manifest.dist;
      const url = new URL(tarball);
      assert.equal(url.protocol, "https:");
      assert.equal(url.host, "registry.npmjs.org");
      assert(!url.username && !url.password && !url.search && !url.hash);
      assert.match(integrity, /^sha512-[A-Za-z0-9+/]{86}==$/);
      const bytes = await pacote.tarball(spec, options);
      assert(ssri.checkData(bytes, entry.integrity), `${spec}: original hash mismatch`);
      assert(ssri.checkData(bytes, integrity), `${spec}: public SHA-512 mismatch`);
      assert.equal(createHash("sha1").update(bytes).digest("hex"), shasum);
      entry.resolved = tarball;
      entry.integrity = integrity;
    }
  };
  await Promise.all(Array.from({ length: 4 }, worker));
  for (const [location, entry] of Object.entries(repaired.packages)) {
    const expected = original.packages[location];
    const { resolved, integrity, ...unchanged } = entry;
    const { resolved: oldResolved, integrity: oldIntegrity, ...oldUnchanged } = expected;
    assert.deepEqual(unchanged, oldUnchanged, `${location}: non-artifact metadata moved`);
    if (!entries.some(([changed]) => changed === location)) {
      assert.equal(resolved, oldResolved);
      assert.equal(integrity, oldIntegrity);
    }
  }
  writeFileSync(path, `${JSON.stringify(repaired, null, 2)}\n`);
  console.log(`Repaired ${entries.length} artifacts using npm's pacote/ssri and verified public tarballs.`);
  console.log("All package versions, platform constraints and native lock records preserved.");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
