// @vitest-environment node
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { Worker } from "node:worker_threads";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const nanoidEntry = require.resolve("nanoid");

const workerSource = `
const { parentPort, workerData } = require("node:worker_threads");
const nanoid = require(workerData.entry);
const generate = workerData.kind === "customAlphabet"
  ? nanoid.customAlphabet("abc", workerData.defaultSize)
  : nanoid.customRandom("abc", workerData.defaultSize, size => new Uint8Array(size));
parentPort.postMessage(
  workerData.requestedSize === null ? generate() : generate(workerData.requestedSize)
);
`;

async function generate(
  kind: string,
  defaultSize: number,
  requestedSize: number | null,
): Promise<string> {
  // The vulnerable version spins forever. A worker keeps that regression
  // bounded and terminable instead of hanging the test runner.
  const worker = new Worker(workerSource, {
    eval: true,
    workerData: { entry: nanoidEntry, kind, defaultSize, requestedSize },
  });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await new Promise<string>((resolve, reject) => {
      timer = setTimeout(() => reject(new Error("nanoid generator did not terminate")), 2000);
      worker.once("message", (value: unknown) => {
        if (typeof value !== "string") {
          reject(new Error("nanoid generator returned a non-string value"));
        } else {
          resolve(value);
        }
      });
      worker.once("error", reject);
      worker.once("exit", (code) => {
        if (code !== 0) reject(new Error(`nanoid worker exited with code ${code}`));
      });
    });
  } finally {
    clearTimeout(timer);
    await worker.terminate();
  }
}

describe("nanoid security contract", () => {
  it("locks compatible patched nanoid instances to public registry artifacts", () => {
    const lock: {
      packages: Record<string, { version?: string; resolved?: string }>;
    } = JSON.parse(readFileSync(new URL("../../package-lock.json", import.meta.url), "utf8"));
    const packages = Object.entries(lock.packages).filter(
      ([path]) => /(?:^|\/)node_modules\/nanoid$/.test(path),
    );
    expect(packages.length).toBeGreaterThan(0);
    for (const [path, pkg] of packages) {
      const version = /^3\.(\d+)\.(\d+)$/.exec(pkg.version ?? "");
      if (!version) throw new Error(`${path}: review the advisory before changing nanoid's major`);
      const minor = Number(version[1]);
      const patch = Number(version[2]);
      expect(minor > 3 || (minor === 3 && patch >= 18), path).toBe(true);
      expect(pkg.resolved).toBe(
        `https://registry.npmjs.org/nanoid/-/nanoid-${pkg.version}.tgz`,
      );
    }
  });

  for (const kind of ["customAlphabet", "customRandom"]) {
    it.each([
      { defaultSize: 0, requestedSize: null, length: 0 },
      { defaultSize: 8, requestedSize: 0, length: 0 },
      { defaultSize: 8, requestedSize: null, length: 8 },
    ])(`${kind} terminates for $defaultSize / $requestedSize`, async (value) => {
      const id = await generate(kind, value.defaultSize, value.requestedSize);
      expect(id).toHaveLength(value.length);
      expect(id).toMatch(/^[abc]*$/);
    });
  }
});
