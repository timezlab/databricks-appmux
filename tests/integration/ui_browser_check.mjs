/**
 * Loads pages of the admin UI in a real Chromium and prints what the browser saw, as one JSON
 * document on stdout. The judging is done by `test_ui_on_container.py`; this file only observes.
 *
 * Usage: node ui_browser_check.mjs '<json>'
 *
 * The JSON argument holds:
 *   webDir        the `web/` folder, where `@playwright/test` is installed (`npm ci`);
 *   baseUrl       where the edge listens, for example http://127.0.0.1:8000;
 *   email         the identity the Databricks proxy would send (X-Forwarded-Email);
 *   pages         [{ name, path, apiPrefix }]: `path` includes any query, `apiPrefix` is the start
 *                 of the API path the page polls, or null for a page that does not poll;
 *   settleSeconds how long a page that does not poll is watched after it loaded;
 *   watchSeconds  how long a page that polls is watched after it loaded.
 *
 * Per page the report holds: the status of the document, the CSP violations the browser raised
 * (a listener is installed before any script of the page runs), page errors, console errors, every
 * request with its time in ms since the load event, and the heading text.
 *
 * Nothing is printed but the report: no header, no cookie and no response body is recorded.
 */
import { createRequire } from "node:module";
import path from "node:path";

const config = JSON.parse(process.argv[2]);
const require = createRequire(path.join(config.webDir, "package.json"));
const { chromium } = require("@playwright/test");

const READY_TIMEOUT_MS = 30_000;

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

async function observePage(browser, entry) {
  const context = await browser.newContext({
    baseURL: config.baseUrl,
    extraHTTPHeaders: { "X-Forwarded-Email": config.email },
  });
  try {
    const page = await context.newPage();
    const consoleErrors = [];
    const pageErrors = [];
    const requests = [];
    let loadedAtMs = null;
    const startedAtMs = Date.now();
    page.on("console", (message) => {
      if (message.type() === "error") {
        consoleErrors.push({ text: message.text(), url: message.location().url });
      }
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("request", (request) => {
      requests.push({
        url: request.url(),
        method: request.method(),
        resourceType: request.resourceType(),
        atMs: Date.now() - startedAtMs,
      });
    });
    await page.addInitScript(installViolationListener);

    const response = await page.goto(entry.path, { waitUntil: "load" });
    loadedAtMs = Date.now() - startedAtMs;
    const status = response === null ? 0 : response.status();
    await page
      .getByRole("heading", { level: 1 })
      .first()
      .waitFor({ timeout: READY_TIMEOUT_MS });
    const watchSeconds =
      entry.apiPrefix === null ? config.settleSeconds : config.watchSeconds;
    await sleep(watchSeconds * 1000);

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
    };
  } finally {
    await context.close();
  }
}

const browser = await chromium.launch();
try {
  const pages = [];
  for (const entry of config.pages) {
    pages.push(await observePage(browser, entry));
  }
  process.stdout.write(JSON.stringify({ pages }));
} finally {
  await browser.close();
}
