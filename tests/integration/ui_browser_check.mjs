/**
 * Loads pages of the admin UI in a real Chromium and prints what the browser saw, as one JSON
 * document on stdout. The judging is done by `test_ui_on_container.py`; this file only observes.
 *
 * Usage: node ui_browser_check.mjs < config.json
 *
 * The configuration is one JSON document on stdin (never an argument: it can hold a bearer token,
 * and arguments show in the process list). It holds:
 *   webDir        the `web/` folder, where `@playwright/test` is installed (`npm ci`);
 *   baseUrl       where the edge or the deployed App answers, for example http://127.0.0.1:8000;
 *   headers       who is calling: the identity headers the Databricks proxy would send, or the
 *                 bearer token of a run through the real proxy. They are added to requests to
 *                 `baseUrl`'s origin only, so a request to any other origin never carries them;
 *   pages         [{ name, path, apiPrefix }]: `path` includes any query, `apiPrefix` is the start
 *                 of the API path the page polls, or null for a page that does not poll;
 *   settleSeconds how long a page that does not poll is watched after it loaded;
 *   watchSeconds  how long a page that polls is watched after it loaded.
 *
 * Per page the report holds: the status of the document, the CSP violations the browser raised
 * (a listener is installed before any script of the page runs), page errors, console errors, every
 * request with its time in ms since the load event, and the heading text.
 *
 * One failure is retried and nothing else: `net::ERR_NETWORK_CHANGED`. Chromium raises it for every
 * request in flight whenever the machine it runs on adds or removes a network interface, and a
 * machine that runs Docker does that all day (a veth pair per container). It says nothing about
 * the page, so a load that met it is thrown away and repeated, up to MAX_NETWORK_CHANGE_RETRIES
 * more times; the report says how many attempts it took (`attempts`) and, when every one met the
 * error, `networkChanged: true`, which the judge reports as no verdict instead of a pass.
 *
 * Nothing is printed but the report: no header, no cookie and no response body is recorded.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const config = JSON.parse(readFileSync(0, "utf8"));
const ownOrigin = new URL(config.baseUrl).origin;
const require = createRequire(path.join(config.webDir, "package.json"));
const { chromium } = require("@playwright/test");

const READY_TIMEOUT_MS = 30_000;
const READY_POLL_MS = 250;
const MAX_NETWORK_CHANGE_RETRIES = 5;
const NETWORK_CHANGED = "net::ERR_NETWORK_CHANGED";

/** Runs in the page before any of its scripts; it must not use anything defined outside it. */
function installViolationListener() {
  const records = [];
  window.__cspViolations = records;
  document.addEventListener(
    "securitypolicyviolation",
    (event) => {
      const target = event.target instanceof Element ? event.target : null;
      records.push({
        effectiveDirective: event.effectiveDirective,
        blockedURI: event.blockedURI,
        sourceFile: event.sourceFile,
        lineNumber: event.lineNumber,
        disposition: event.disposition,
        target:
          target === null
            ? "document"
            : target.outerHTML.slice(0, 140).replace(/\s+/g, " "),
      });
    },
    true,
  );
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** The report for a load the machine's network change cut short: the same shape, nothing seen. */
function cutShort(entry) {
  return {
    name: entry.name,
    path: entry.path,
    apiPrefix: entry.apiPrefix,
    status: 0,
    heading: "",
    loadedAtMs: null,
    watchedSeconds: 0,
    violations: [],
    pageErrors: [],
    consoleErrors: [],
    requests: [],
    networkChanged: true,
  };
}

async function observePage(browser, entry) {
  const context = await browser.newContext({ baseURL: config.baseUrl });
  // Not `extraHTTPHeaders`: that would send the credential to every origin the page calls, and a
  // page that calls another origin is exactly what this check is looking for.
  await context.route("**/*", (route) => {
    const request = route.request();
    if (new URL(request.url()).origin === ownOrigin) {
      route.continue({ headers: { ...request.headers(), ...config.headers } });
    } else {
      route.continue();
    }
  });
  try {
    const page = await context.newPage();
    const consoleErrors = [];
    const pageErrors = [];
    const requests = [];
    let loadedAtMs = null;
    let networkChanged = false;
    const startedAtMs = Date.now();
    page.on("console", (message) => {
      if (message.type() === "error") {
        consoleErrors.push({ text: message.text(), url: message.location().url });
      }
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("requestfailed", (request) => {
      if (request.failure()?.errorText === NETWORK_CHANGED) {
        networkChanged = true;
      }
    });
    page.on("request", (request) => {
      requests.push({
        url: request.url(),
        method: request.method(),
        resourceType: request.resourceType(),
        atMs: Date.now() - startedAtMs,
      });
    });
    await page.addInitScript(installViolationListener);

    let response = null;
    const watchSeconds =
      entry.apiPrefix === null ? config.settleSeconds : config.watchSeconds;
    try {
      response = await page.goto(entry.path, { waitUntil: "load" });
      loadedAtMs = Date.now() - startedAtMs;
      // Not `locator.waitFor`: it would sit out its whole timeout on a page whose scripts the
      // network change cut off, when the retry could already be running.
      const heading = page.getByRole("heading", { level: 1 }).first();
      const readyDeadlineMs = Date.now() + READY_TIMEOUT_MS;
      while (!networkChanged && !(await heading.isVisible())) {
        if (Date.now() > readyDeadlineMs) {
          throw new Error(`no level-1 heading became visible within ${READY_TIMEOUT_MS} ms`);
        }
        await sleep(READY_POLL_MS);
      }
      await sleep(watchSeconds * 1000);
    } catch (error) {
      // A load cut short by the machine's own network change is retried by the caller; any other
      // failure is a finding and ends the run as before.
      if (networkChanged || String(error.message).includes(NETWORK_CHANGED)) {
        return cutShort(entry);
      }
      throw error;
    }
    if (networkChanged) {
      return cutShort(entry);
    }
    const status = response === null ? 0 : response.status();

    const violations = await page.evaluate(() => window.__cspViolations);
    const heading = await page.getByRole("heading", { level: 1 }).first().innerText();
    return {
      name: entry.name,
      path: entry.path,
      apiPrefix: entry.apiPrefix,
      status,
      heading,
      loadedAtMs,
      watchedSeconds: watchSeconds,
      violations,
      pageErrors,
      consoleErrors,
      requests,
      networkChanged: false,
    };
  } finally {
    await context.close();
  }
}

async function observePageSteadily(browser, entry) {
  let report = null;
  let attempts = 0;
  do {
    attempts += 1;
    report = await observePage(browser, entry);
  } while (report.networkChanged && attempts <= MAX_NETWORK_CHANGE_RETRIES);
  return { ...report, attempts };
}

const browser = await chromium.launch();
try {
  const pages = [];
  for (const entry of config.pages) {
    pages.push(await observePageSteadily(browser, entry));
  }
  process.stdout.write(JSON.stringify({ pages }));
} finally {
  await browser.close();
}
