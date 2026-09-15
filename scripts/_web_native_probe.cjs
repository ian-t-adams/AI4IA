"use strict";

const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { readFileSync, realpathSync, statSync } = require("node:fs");
const { createRequire } = require("node:module");
const path = require("node:path");
const vm = require("node:vm");

try {
  const [web, expectedHash] = process.argv.slice(2);
  assert.equal(process.argv.length, 4);
  assert.match(expectedHash, /^[a-f0-9]{64}$/);
  assert.equal(process.versions.node, "22.23.2");
  assert.equal(process.platform, "win32");
  assert.equal(process.arch, "x64");
  const root = realpathSync(web);
  const localRequire = createRequire(path.join(root, "package.json"));
  const name = "@next/swc-win32-x64-msvc";
  const packageRoot = realpathSync(path.join(root, "node_modules", "@next", "swc-win32-x64-msvc"));
  assert.equal(packageRoot, path.join(root, "node_modules", "@next", "swc-win32-x64-msvc"));
  const manifest = JSON.parse(readFileSync(path.join(packageRoot, "package.json"), "utf8"));
  assert.equal(manifest.name, name);
  assert.equal(manifest.version, "16.3.5");
  assert.deepEqual(manifest.os, ["win32"]);
  assert.deepEqual(manifest.cpu, ["x64"]);
  assert.equal(path.basename(manifest.main), manifest.main);
  assert.equal(path.extname(manifest.main), ".node");
  const binary = realpathSync(localRequire.resolve(name));
  assert.equal(binary, path.join(packageRoot, manifest.main));
  assert.ok(statSync(binary).size <= 512 * 1024 * 1024);
  const binaryHash = createHash("sha256").update(readFileSync(binary)).digest("hex");
  assert.equal(binaryHash, expectedHash);

  // Load the verified native file directly, never Next's download/WASM fallback.
  const binding = localRequire(binary);
  const transformed = binding.transformSync(
    "export const answer: number = 42;",
    false,
    Buffer.from(JSON.stringify({
      filename: "native-control.ts",
      jsc: { parser: { syntax: "typescript" }, target: "es2020" },
      module: { type: "commonjs" },
    })),
  );
  assert.equal(typeof transformed.code, "string");
  assert.ok(transformed.code.length > 0 && transformed.code.length <= 4096);
  const result = { exports: {} };
  vm.runInNewContext(transformed.code, result, { timeout: 1000 });
  assert.equal(result.exports.answer, 42);
  process.stdout.write(JSON.stringify({
    native: true,
    node: process.versions.node,
    platform: process.platform,
    arch: process.arch,
    name,
    version: manifest.version,
    binarySha256: binaryHash,
    result: result.exports.answer,
  }));
} catch {
  process.stderr.write("native_control_failed\n");
  process.exitCode = 1;
}
