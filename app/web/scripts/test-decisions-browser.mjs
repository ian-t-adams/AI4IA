// Optional test-only runner: the browser, Next proxy, FastAPI route, catalog,
// request/response validation and in-memory usage are real. Only native model
// HTTP is stubbed by tests.decision_preview. No shipping runtime switch.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.AI4IA_PLAYWRIGHT_MODULE || "playwright");
const web = fileURLToPath(new URL("..", import.meta.url));
const api = path.resolve(web, "..", "api");
const python = process.env.AI4IA_TEST_PYTHON || path.join(
  api, ".venv", process.platform === "win32" ? "Scripts\\python.exe" : "bin/python",
);
const interactive = process.argv.includes("--interactive");
const processes = [];
let browser;

async function port() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const number = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return number;
}

async function start(command, args, cwd, env, ready) {
  const child = spawn(command, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
  processes.push(child);
  let output = "";
  let failure;
  child.on("error", (error) => { failure = error; });
  for (const stream of [child.stdout, child.stderr]) {
    stream.on("data", (chunk) => { output = (output + chunk).slice(-8000); });
  }
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (failure) throw failure;
    if (child.exitCode !== null) throw new Error(`Preview exited: ${output}`);
    try {
      if ((await fetch(ready, { signal: AbortSignal.timeout(1000), redirect: "error" })).ok) return;
    } catch { /* Startup is bounded; a failed health read is not readiness. */ }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Preview startup timed out: ${output}`);
}

try {
  const apiPort = await port();
  const webPort = await port();
  const origin = `http://127.0.0.1:${webPort}`;
  await start(python, ["-m", "tests.decision_preview", "--port", String(apiPort)], api,
    process.env, `http://127.0.0.1:${apiPort}/health/live`);
  await start(process.execPath, ["node_modules/next/dist/bin/next", "start", "-H", "127.0.0.1", "-p", String(webPort)],
    web, { ...process.env, API_BASE_URL: `http://127.0.0.1:${apiPort}`, DEV_USER: "decision-preview",
      WEB_AUTH_PROVIDER: "dev", NEXT_TELEMETRY_DISABLED: "1" }, `${origin}/build-id`);
  browser = await chromium.launch({
    headless: !interactive,
    ...(process.env.AI4IA_BROWSER_CHANNEL ? { channel: process.env.AI4IA_BROWSER_CHANNEL } : {}),
  });
  const context = await browser.newContext({ reducedMotion: "reduce" });
  const page = await context.newPage();
  const unexpected = [];
  const runs = [];
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/decisions/run") runs.push(request);
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || (route.request().method() !== "GET" && url.pathname !== "/api/decisions/run")) {
      unexpected.push(`${route.request().method()} ${url.origin}${url.pathname}`);
      return route.abort();
    }
    return route.continue();
  });
  await page.goto(origin);
  await page.getByRole("button", { name: "Decisions", exact: true }).click();
  assert.equal(new URL(page.url()).hash, "#/decisions");
  await page.getByRole("button", { name: "Load synthetic example" }).click();
  assert.equal(runs.length, 0, "Options and example loading must not run a model");
  await page.getByRole("button", { name: "Run decision", exact: true }).click();
  const results = page.getByRole("region", { name: "Decision results", exact: true });
  await results.waitFor();
  assert.match(await results.innerText(), /Yes probability: 75.0%/);
  assert.match(await results.innerText(), /Selected: design/);
  assert.match(await results.innerText(), /Score: 0.000 on a 0.2 scale/);
  assert.match(await results.innerText(), /123 input \/ 7 output \/ 130 total/);
  assert.match(await results.innerText(), /Cost: Unknown/);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].postDataJSON().model, "Microsoft-Decision-1");

  await page.getByRole("textbox", { name: "Content to evaluate" }).fill("fixture:upstream-error");
  await page.getByRole("button", { name: "Run decision", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: /Decision request failed/i }).waitFor();
  assert.equal(runs.length, 2, "Failed native requests must not automatically retry");
  assert.equal(await results.count(), 0, "A failed run must not display stale success");
  await page.getByRole("textbox", { name: "Content to evaluate" }).fill('{"synthetic":true}');
  await page.getByRole("combobox", { name: /Input format/ }).selectOption("json");
  await page.getByRole("button", { name: "Run decision", exact: true }).click();
  await results.waitFor();
  assert.equal(runs.length, 3);

  // Only these option-error/empty fixtures are browser-intercepted; successful
  // options and all Run requests above traversed the real same-origin API.
  await page.route("**/api/decisions/options", (route) => route.fulfill({
    status: 503, json: { detail: "Synthetic options outage" },
  }));
  await page.reload();
  await page.getByRole("button", { name: "Reload availability" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Run decision", exact: true }).isEnabled(), false);
  await page.unroute("**/api/decisions/options");
  await page.getByRole("button", { name: "Reload availability" }).click();
  await page.getByRole("button", { name: "Load synthetic example" }).waitFor();
  await page.getByRole("button", { name: "Load synthetic example" }).click();
  assert.equal(await page.getByRole("button", { name: "Run decision", exact: true }).isEnabled(), true);
  await page.route("**/api/decisions/options", (route) => route.fulfill({ json: { models: [] } }));
  await page.reload();
  await page.getByText("No Decision model is available under the current model and residency policy.").waitFor();
  assert.equal(await page.getByRole("button", { name: "Decisions", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Run decision", exact: true }).isEnabled(), false);
  assert.equal(runs.length, 3);
  assert.deepEqual(unexpected, [], "No external traffic or unrelated write is allowed");
  assert.deepEqual(pageErrors, []);
  console.log("PASS: real browser -> Next proxy -> FastAPI -> native transport fixture; text/JSON, all answer types, failure/recovery, availability gating.");
  if (interactive) {
    await page.unroute("**/api/decisions/options");
    await page.reload();
    console.log("Offline playground ready. Use Load synthetic example / Run decision. Input fixture:upstream-error produces a visible failure. Resume in the Inspector to finish.");
    await page.pause();
  }
} finally {
  await browser?.close();
  for (const child of processes.reverse()) {
    if (child.exitCode === null) {
      const exited = new Promise((resolve) => child.once("exit", resolve));
      child.kill();
      await exited;
    }
  }
}
