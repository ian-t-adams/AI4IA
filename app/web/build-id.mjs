// One identifier per web build, derived from the inputs that shape the browser
// bundle. next.config.mjs inlines it into both the client bundle and the public
// /build-id route, so an open tab can tell that a newer build is deployed.
//
// A content hash rather than a random value: every evaluation of the config in
// one build agrees, rebuilding identical sources (an API-only deploy rebuilds
// this image too) yields the same id and prompts no reload, and any change to
// the sources, lockfile or config yields a new one. Test files never reach the
// bundle, so they are left out.
import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const INPUTS = [
  "src",
  "public",
  "package.json",
  "package-lock.json",
  "next.config.mjs",
  "build-id.mjs",
  "tsconfig.json",
];
const TEST_FILE = /\.test\.[cm]?[jt]sx?$/;

function* files(root, relativePath) {
  const path = join(root, relativePath);
  if (!existsSync(path)) return;
  if (statSync(path).isDirectory()) {
    for (const child of readdirSync(path).sort()) {
      yield* files(root, `${relativePath}/${child}`);
    }
  } else if (!TEST_FILE.test(relativePath)) {
    yield relativePath;
  }
}

export function webBuildId(root) {
  const hash = createHash("sha256");
  for (const input of INPUTS) {
    for (const file of files(root, input)) {
      hash.update(file);
      hash.update("\0");
      hash.update(readFileSync(join(root, file)));
      hash.update("\0");
    }
  }
  return hash.digest("hex").slice(0, 20);
}
