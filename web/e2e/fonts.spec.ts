/**
 * The self-hosted IBM Plex files (src/app/fonts.css) load in a real Chromium
 * under the reference CSP (`font-src 'self'`, e2e/server.ts), from the export's
 * own origin, and nothing about them is refused. A font the policy blocked would
 * fall back to the system face without a visible error, so this looks at what
 * the browser actually loaded.
 *
 * `npm run build` first; `npm run test:e2e` runs this.
 */
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { PAGES, applyScenario, waitForPageReady } from "./pages.ts";
import type { PageEntry } from "./pages.ts";
import { BASE_PATH, startServer } from "./server.ts";
import type { E2eServer } from "./server.ts";

/** Where the build puts the files a stylesheet's url() names. */
const MEDIA_PREFIX = `${BASE_PATH}/_next/static/media/`;

let server: E2eServer;

test.beforeAll(async () => {
  server = await startServer();
});
test.afterAll(async () => {
  await server.close();
});

/** The launcher (sans, and the route strip in mono 500) and the app's overview (mono 400 for the source folder). */
const FONT_PAGES = ["launcher", "app-overview"] as const;

function pageEntry(name: string): PageEntry {
  const entry = PAGES.find((candidate) => candidate.name === name);
  if (entry === undefined) throw new Error(`No page entry named ${name}`);
  return entry;
}

/** Runs in the page before its scripts: records every CSP violation, in the capture phase so nothing can stop it first. */
function installViolationListener(): void {
  const records: string[] = [];
  (window as unknown as { __cspViolations: string[] }).__cspViolations =
    records;
  document.addEventListener(
    "securitypolicyviolation",
    (event) => {
      records.push(`${event.effectiveDirective} blocked=${event.blockedURI}`);
    },
    true,
  );
}

interface FontResponse {
  url: string;
  status: number;
  contentType: string;
}

interface LoadedFace {
  family: string;
  weight: string;
  status: string;
}

async function openAndCollect(
  page: Page,
  entry: PageEntry,
): Promise<{ responses: FontResponse[]; faces: LoadedFace[] }> {
  const responses: FontResponse[] = [];
  page.on("response", (response) => {
    if (response.request().resourceType() !== "font") return;
    responses.push({
      url: response.url(),
      status: response.status(),
      contentType: response.headers()["content-type"] ?? "",
    });
  });
  await page.addInitScript(installViolationListener);
  await applyScenario(page, entry);
  await page.goto(`${server.origin}${entry.path}`);
  await waitForPageReady(page, entry);
  const faces = await page.evaluate(async () => {
    await document.fonts.ready;
    return [...document.fonts].map((face) => ({
      family: face.family.replaceAll('"', ""),
      weight: face.weight,
      status: face.status,
    }));
  });
  return { responses, faces };
}

for (const name of FONT_PAGES) {
  test(`${name}: IBM Plex loads from the export under the CSP`, async ({
    page,
  }) => {
    const entry = pageEntry(name);
    const { responses, faces } = await openAndCollect(page, entry);

    const violations = await page.evaluate(
      () =>
        (window as unknown as { __cspViolations: string[] }).__cspViolations,
    );
    expect(violations).toEqual([]);

    // Every font request went to the export's own media folder and was served as woff2.
    expect(responses.length).toBeGreaterThan(0);
    for (const response of responses) {
      const url = new URL(response.url);
      expect(url.origin).toBe(server.origin);
      expect(url.pathname.startsWith(MEDIA_PREFIX)).toBe(true);
      expect(url.pathname.endsWith(".woff2")).toBe(true);
      expect(response.status).toBe(200);
      expect(response.contentType).toBe("font/woff2");
    }

    // The browser loaded both faces; none of the declared ones failed.
    const loadedFamilies = new Set(
      faces
        .filter((face) => face.status === "loaded")
        .map((face) => face.family),
    );
    expect(loadedFamilies).toEqual(new Set(["IBM Plex Sans", "IBM Plex Mono"]));
    expect(faces.filter((face) => face.status === "error")).toEqual([]);

    // The interface text is drawn in Plex, not the fallback.
    const bodyFamily = await page.evaluate(
      () => getComputedStyle(document.body).fontFamily,
    );
    expect(bodyFamily.startsWith('"IBM Plex Sans"')).toBe(true);
  });
}
