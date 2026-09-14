import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

type LockedPackage = {
  version?: string;
  resolved?: string;
  integrity?: string;
  optionalDependencies?: Record<string, string>;
};

type PackageLock = {
  lockfileVersion: number;
  packages: Record<string, LockedPackage>;
};

const lock: PackageLock = JSON.parse(
  readFileSync(new URL("../../package-lock.json", import.meta.url), "utf8"),
);
const manifest: { dependencies: { next: string } } = JSON.parse(
  readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
);

function declaredBinaries(value: PackageLock): [string, string][] {
  const optional = value.packages["node_modules/next"]?.optionalDependencies;
  const binaries = Object.entries(optional ?? {}).filter(([name]) => name.startsWith("@next/swc-"));
  if (binaries.length === 0) throw new Error("Next declares no native SWC packages; review the lock format.");
  return binaries;
}

function resolutionPaths(name: string): string[] {
  return [`node_modules/next/node_modules/${name}`, `node_modules/${name}`];
}

function assertNativeCoverage(value: PackageLock): void {
  for (const [name, version] of declaredBinaries(value)) {
    const entry = resolutionPaths(name).map((path) => value.packages[path]).find(Boolean);
    if (!entry) throw new Error(`Missing locked native package: ${name}`);
    if (entry.version !== version) throw new Error(`Native package version mismatch: ${name}`);
    const basename = name.slice("@next/".length);
    if (entry.resolved !== `https://registry.npmjs.org/${name}/-/${basename}-${version}.tgz`) {
      throw new Error(`Native package public artifact reference is missing or inconsistent: ${name}`);
    }
    if (!/^sha512-[A-Za-z0-9+/]{86}==$/.test(entry.integrity ?? "")) {
      throw new Error(`Native package integrity metadata is missing or malformed: ${name}`);
    }
  }
}

describe("Next native lock coverage", () => {
  it("locks every declared platform, not only the CI runner's platform", () => {
    expect(lock.lockfileVersion).toBe(3);
    expect(lock.packages["node_modules/next"]?.version).toBe(manifest.dependencies.next);
    assertNativeCoverage(lock);
  });

  it.each(declaredBinaries(lock))("rejects a missing %s record while the declaration remains", (name) => {
    assertNativeCoverage(lock);
    const incomplete = structuredClone(lock);
    for (const path of resolutionPaths(name)) delete incomplete.packages[path];
    expect(() => assertNativeCoverage(incomplete)).toThrow(`Missing locked native package: ${name}`);
    assertNativeCoverage(lock);
  });

  it("does not accept a different version, missing integrity, or mismatched artifact", () => {
    const [name] = declaredBinaries(lock)[0];
    const path = resolutionPaths(name).find((candidate) => lock.packages[candidate]);
    if (!path) throw new Error(`Missing locked native package: ${name}`);
    for (const change of [
      { version: "0.0.0" },
      { integrity: "" },
      { resolved: "https://example.invalid/native.tgz" },
    ]) {
      const changed = structuredClone(lock);
      changed.packages[path] = { ...changed.packages[path], ...change };
      expect(() => assertNativeCoverage(changed)).toThrow(/Native package/);
      assertNativeCoverage(lock);
    }
  });

  it("refuses vacuous coverage when the native declarations disappear", () => {
    const changed = structuredClone(lock);
    changed.packages["node_modules/next"].optionalDependencies = {};
    expect(() => assertNativeCoverage(changed)).toThrow("Next declares no native SWC packages");
    assertNativeCoverage(lock);
  });

  it("accepts a nested resolved package but not an unrelated sibling's copy", () => {
    const [name] = declaredBinaries(lock)[0];
    const [nested, root] = resolutionPaths(name);
    const changed = structuredClone(lock);
    const entry = changed.packages[nested] ?? changed.packages[root];
    delete changed.packages[root];
    changed.packages[nested] = entry;
    assertNativeCoverage(changed);
    delete changed.packages[nested];
    changed.packages[`node_modules/unrelated/node_modules/${name}`] = entry;
    expect(() => assertNativeCoverage(changed)).toThrow(`Missing locked native package: ${name}`);
    assertNativeCoverage(lock);
  });
});
