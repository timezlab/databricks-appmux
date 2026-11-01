/**
 * What the spec and the screenshot script share: the pages to load, the sizes
 * and colour schemes to load them in, and how to know a page has finished
 * drawing. Defined once so the two cannot drift apart.
 */
import type { Page } from "@playwright/test";

import type { Scenario } from "./fixtures.ts";
import { API_PREFIX, BASE_PATH } from "./server.ts";

export interface PageEntry {
  name: string;
  /** Path and query, under the base path. */
  path: string;
  /** Status of the document itself. */
  status: number;
  /** The text of the page's `<h1>`. */
  heading: string;
  /** Text that only appears once the page has its data from the mock API. */
  dataText: string;
  /** The API call that fills the page (path and query prefix), or null for a page with no data. */
  apiCall: string | null;
  /** The world the mock API answers from (fixtures.ts); the default is the contract's examples. */
  scenario?: Scenario;
  /** Puts the page in the state to look at (edits typed, a save pressed) once it has its data. */
  act?: (page: Page) => Promise<void>;
  /** The page is meant to show a failure (a save that did not go through), so the check for failure texts is skipped. */
  isFailureShown?: boolean;
}

/** The example app of the contract (`AppList.json`, `AppDetail.json`). */
export const EXAMPLE_APP_SLUG = "sales";

const APP_API_CALL = `${API_PREFIX}/apps/${EXAMPLE_APP_SLUG}`;
const PLATFORM_API_CALL = `${API_PREFIX}/platform`;

/** The settings the two "settings" entries edit (current values: 120, 100, 64). */
const EDITED_SETTINGS: Readonly<Record<string, string>> = {
  request_timeout_s: "150",
  max_request_body_mb: "200",
  max_concurrent_requests: "128",
};

async function typeThreeSettings(page: Page): Promise<void> {
  for (const [key, value] of Object.entries(EDITED_SETTINGS)) {
    await page.getByLabel(key, { exact: true }).fill(value);
  }
  await page
    .getByRole("region", { name: "Unsaved changes" })
    .getByText("3 settings changed, not saved yet")
    .waitFor({ timeout: READY_TIMEOUT_MS });
}

async function typeThreeSettingsAndSave(page: Page): Promise<void> {
  await typeThreeSettings(page);
  await page.getByRole("button", { name: "Save changes" }).click();
  await page
    .getByRole("region", { name: "Unsaved changes" })
    .getByText(/2 of 3 saved, 1 failed/)
    .waitFor({ timeout: READY_TIMEOUT_MS });
}

/**
 * Every page of the export. The sub-pages of the app and of the platform are
 * routes of their own with their own inline scripts, so each is loaded, checked
 * against the CSP and photographed like the first-level ones.
 */
export const PAGES: readonly PageEntry[] = [
  {
    name: "launcher",
    path: `${BASE_PATH}/`,
    status: 200,
    heading: "Apps",
    dataText: "Sales dashboard",
    apiCall: `${API_PREFIX}/apps`,
  },
  {
    name: "app-status",
    path: `${BASE_PATH}/app/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    // The heading is the display name, so it is data too.
    heading: "Sales dashboard",
    dataText: `${EXAMPLE_APP_SLUG}, owned by alice@example.com`,
    apiCall: APP_API_CALL,
  },
  {
    name: "app-releases",
    path: `${BASE_PATH}/app/releases/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "fix pagination",
    apiCall: APP_API_CALL,
  },
  {
    name: "app-resources",
    path: `${BASE_PATH}/app/resources/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "forecast-model",
    apiCall: APP_API_CALL,
  },
  {
    name: "app-sharing",
    path: `${BASE_PATH}/app/sharing/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "carol@example.com",
    apiCall: APP_API_CALL,
  },
  {
    name: "app-requests",
    path: `${BASE_PATH}/app/requests/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "Requests to a platform admin",
    apiCall: APP_API_CALL,
  },
  {
    name: "platform-overview",
    path: `${BASE_PATH}/platform/`,
    status: 200,
    heading: "Platform",
    dataText: "Their reason",
    apiCall: PLATFORM_API_CALL,
  },
  {
    name: "platform-apps",
    path: `${BASE_PATH}/platform/apps/`,
    status: 200,
    heading: "Platform",
    dataText: "Register an app",
    apiCall: PLATFORM_API_CALL,
  },
  {
    name: "platform-access",
    path: `${BASE_PATH}/platform/access/`,
    status: 200,
    heading: "Platform",
    dataText: "data-platform",
    apiCall: PLATFORM_API_CALL,
  },
  {
    name: "platform-egress",
    path: `${BASE_PATH}/platform/egress/`,
    status: 200,
    heading: "Platform",
    dataText: "Global egress rules",
    apiCall: PLATFORM_API_CALL,
  },
  {
    name: "platform-settings",
    path: `${BASE_PATH}/platform/settings/`,
    status: 200,
    heading: "Platform",
    dataText: "memory_reserve_mb",
    apiCall: PLATFORM_API_CALL,
  },
  // The same screens in other worlds (fixtures.ts): the ones a design review
  // asks to see, which the contract's examples (a platform admin with three
  // apps) never show.
  {
    name: "launcher-visitor",
    path: `${BASE_PATH}/`,
    status: 200,
    heading: "Apps",
    dataText: "Sales forecast",
    apiCall: `${API_PREFIX}/apps`,
    scenario: "visitor",
  },
  {
    name: "launcher-requester",
    path: `${BASE_PATH}/`,
    status: 200,
    heading: "Apps",
    dataText: "Your requests",
    apiCall: `${API_PREFIX}/apps`,
    scenario: "requester",
  },
  {
    name: "platform-memory-low",
    path: `${BASE_PATH}/platform/`,
    status: 200,
    heading: "Platform",
    dataText: "MB committed",
    apiCall: PLATFORM_API_CALL,
    scenario: "memory-low",
  },
  {
    name: "platform-memory-warning",
    path: `${BASE_PATH}/platform/`,
    status: 200,
    heading: "Platform",
    dataText: "MB committed",
    apiCall: PLATFORM_API_CALL,
    scenario: "memory-warning",
  },
  {
    name: "platform-memory-danger",
    path: `${BASE_PATH}/platform/`,
    status: 200,
    heading: "Platform",
    dataText: "MB committed",
    apiCall: PLATFORM_API_CALL,
    scenario: "memory-danger",
  },
  {
    name: "platform-settings-changed",
    path: `${BASE_PATH}/platform/settings/`,
    status: 200,
    heading: "Platform",
    dataText: "memory_reserve_mb",
    apiCall: PLATFORM_API_CALL,
    act: typeThreeSettings,
  },
  {
    name: "platform-settings-save-failed",
    path: `${BASE_PATH}/platform/settings/`,
    status: 200,
    heading: "Platform",
    dataText: "memory_reserve_mb",
    apiCall: PLATFORM_API_CALL,
    scenario: "settings-one-fails",
    act: typeThreeSettingsAndSave,
    isFailureShown: true,
  },
  {
    name: "dashboard",
    path: `${BASE_PATH}/dashboard/`,
    status: 200,
    heading: "Dashboard",
    // A chart title: it appears when the metrics answer has points to draw.
    dataText: "Requests per minute",
    apiCall: `${API_PREFIX}/metrics`,
  },
];

/** A path no page of the export answers: the edge serves the export's 404 page for it. */
export const NOT_FOUND_PAGE: PageEntry = {
  name: "not-found",
  path: `${BASE_PATH}/no-such-page/`,
  status: 404,
  heading: "Page not found",
  dataText: "There is no such page in the admin UI",
  apiCall: null,
};

export const VIEWPORTS = [
  { width: 1280, height: 800 },
  { width: 390, height: 844 },
] as const;

export const COLOR_SCHEMES = ["light", "dark"] as const;
export type ColorScheme = (typeof COLOR_SCHEMES)[number];

/** Longer than a page needs (about a second) and shorter than the 30 s test timeout, so a page that never loads fails with its own message. */
export const READY_TIMEOUT_MS = 10_000;

/** Makes every request of `page` ask for the entry's world; call before `goto`. */
export async function applyScenario(
  page: Page,
  entry: PageEntry,
): Promise<void> {
  if (entry.scenario === undefined) return;
  await page.setExtraHTTPHeaders({ "X-E2E-Scenario": entry.scenario });
}

/**
 * Waits until the page shows its heading and its data, puts it in the state the
 * entry wants to show, then waits until the network is quiet and two frames
 * have been drawn, so a late style or script has had its chance to be refused
 * before the caller looks.
 */
export async function waitForPageReady(
  page: Page,
  entry: PageEntry,
): Promise<void> {
  await page
    .getByRole("heading", { level: 1, name: entry.heading, exact: true })
    .waitFor({ timeout: READY_TIMEOUT_MS });
  // `innerText` is the rendered text; a hidden <option> or a script does not count.
  await page.waitForFunction(
    (text) => document.querySelector("main")?.innerText.includes(text) ?? false,
    entry.dataText,
    { timeout: READY_TIMEOUT_MS },
  );
  await page.waitForLoadState("networkidle", { timeout: READY_TIMEOUT_MS });
  if (entry.act !== undefined) await entry.act(page);
  await page.waitForLoadState("networkidle", { timeout: READY_TIMEOUT_MS });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            resolve();
          });
        });
      }),
  );
}
