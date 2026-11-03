/**
 * What the spec and the screenshot script share: the pages to load, the sizes
 * and colour schemes to load them in, and how to know a page has finished
 * drawing. Defined once so the two cannot drift apart.
 */
import type { Page } from "@playwright/test";

import type { Scenario } from "./fixtures.ts";
import { NEW_LOG_LINE_TEXT, NODE_FAILURE_HINT } from "./fixtures-f1b.ts";
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

/** Waits for the failed target's output (open from the start), then opens its row menu (a portalled, non-modal menu). */
async function openFailedReleaseMenu(page: Page): Promise<void> {
  await page
    .getByRole("region", { name: "Output of release 8" })
    .waitFor({ timeout: READY_TIMEOUT_MS });
  await page.getByRole("button", { name: "Actions for release 8" }).click();
  await page
    .getByRole("menuitem", { name: /Activate/ })
    .waitFor({ timeout: READY_TIMEOUT_MS });
}

/** The child prints its first lines: the next poll (every 2 s) brings them. */
async function receiveNewLogLines(page: Page): Promise<void> {
  await page.setExtraHTTPHeaders({ "X-E2E-Scenario": "logs-new-lines" });
  await page
    .getByText(NEW_LOG_LINE_TEXT, { exact: true })
    .waitFor({ timeout: READY_TIMEOUT_MS });
}

async function openPackagesTab(page: Page): Promise<void> {
  await page.getByRole("tab", { name: /^Packages/ }).click();
  await page
    .getByRole("tabpanel", { name: /^Packages/ })
    .getByText("databricks-sdk")
    .waitFor({ timeout: READY_TIMEOUT_MS });
}

async function showEnvironmentJson(page: Page): Promise<void> {
  await page.getByRole("radio", { name: "JSON" }).click();
  await page
    .getByRole("region", { name: "Environment as JSON" })
    .waitFor({ timeout: READY_TIMEOUT_MS });
}

/** The titles the dashboard's first charts take over 7 days: 3-hour columns. */
const WEEK_CHART_TITLES = [
  "Requests per 3 hours",
  "Requests by app, per 3 hours",
] as const;

/**
 * Presses 7d once the hour is drawn. The mock's default metrics stop at a day,
 * as the API did before `step`, and a world of fixtures-f4.ts answers one window
 * whatever the query, so the week's world is asked for from here on, not from
 * the first call (the hour before it would get a week).
 */
async function pressSevenDays(page: Page): Promise<void> {
  await page.setExtraHTTPHeaders({
    "X-E2E-Scenario": "metrics-week" satisfies Scenario,
  });
  await page.getByRole("radio", { name: /^7d,/ }).click();
  for (const title of WEEK_CHART_TITLES) {
    await page
      .getByRole("group", { name: title, exact: true })
      .waitFor({ timeout: READY_TIMEOUT_MS });
  }
}

/** Withdraws the visitor's one pending request (scenario "requester"); the mock answers it closed. */
async function withdrawPendingRequest(page: Page): Promise<void> {
  const row = page.getByRole("row", { name: /Team wiki 2/ });
  await row.getByRole("button", { name: /^Withdraw/ }).click();
  // The live region's copy of the notice: the notice itself has no role.
  await page
    .getByRole("status")
    .filter({ hasText: /^Withdrew your request for/ })
    .waitFor({ timeout: READY_TIMEOUT_MS });
  await row.getByText("Withdrawn").waitFor({ timeout: READY_TIMEOUT_MS });
}

/** Opens Browse beside the register form's source folder; the mock answers every folder with the example listing, a Node.js app. */
async function openFolderBrowser(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Browse", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Browse the workspace" })
    .getByText(/looks like a Node\.js app/)
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
  // The ten sections of one app, in sidebar order. The heading is the display
  // name, so it is data too; every section reads the app through the shell.
  {
    name: "app-overview",
    path: `${BASE_PATH}/app/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: `${EXAMPLE_APP_SLUG}, owned by alice@example.com`,
    apiCall: APP_API_CALL,
  },
  {
    name: "app-authorization",
    path: `${BASE_PATH}/app/authorization/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "carol@example.com",
    apiCall: APP_API_CALL,
  },
  {
    name: "app-deployments",
    path: `${BASE_PATH}/app/deployments/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "fix pagination",
    apiCall: APP_API_CALL,
  },
  {
    name: "app-logs",
    path: `${BASE_PATH}/app/logs/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "Traceback (most recent call last):",
    apiCall: APP_API_CALL,
  },
  {
    name: "app-security",
    path: `${BASE_PATH}/app/security/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    // The caption of the stored global rules, which only a platform admin's `GET /platform` fills.
    dataText: "The global rules, as stored",
    apiCall: APP_API_CALL,
  },
  {
    name: "app-environment",
    path: `${BASE_PATH}/app/environment/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "APPMUX_BASE_PATH",
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
    name: "app-insights",
    path: `${BASE_PATH}/app/insights/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    // A chart title: it appears when the app's metrics have points to draw.
    dataText: "Requests per minute",
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
    name: "app-settings",
    path: `${BASE_PATH}/app/settings/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    // The platform admin's form, once `/me` has said who is looking.
    dataText: "Nothing changed yet",
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
    // The check that the page "read" apiCall is the check that Withdraw was sent and answered.
    name: "launcher-requester-withdrawn",
    path: `${BASE_PATH}/`,
    status: 200,
    heading: "Apps",
    dataText: "Your requests",
    apiCall: `${API_PREFIX}/requests/24/withdraw`,
    scenario: "requester",
    act: withdrawPendingRequest,
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
    name: "platform-apps-browse",
    path: `${BASE_PATH}/platform/apps/`,
    status: 200,
    heading: "Platform",
    dataText: "Register an app",
    apiCall: PLATFORM_API_CALL,
    act: openFolderBrowser,
  },
  {
    name: "dashboard",
    path: `${BASE_PATH}/dashboard/`,
    status: 200,
    heading: "Dashboard",
    // The breakdown's first chart title: it appears when the breakdown has
    // points to draw (the platform's charts above it read the default rows).
    dataText: "Requests by app, per minute",
    apiCall: `${API_PREFIX}/metrics`,
    scenario: "breakdown-live",
  },
  {
    name: "dashboard-7-days",
    path: `${BASE_PATH}/dashboard/`,
    status: 200,
    heading: "Dashboard",
    // The platform's first chart title, over the hour ("per minute") and then
    // over the week ("per 3 hours").
    dataText: "Requests per",
    apiCall: `${API_PREFIX}/metrics`,
    act: pressSevenDays,
  },
  // Authorization and Resources mid-task (lane F2): a name typed into Who
  // with the directory's suggestions open; the Add resource panel with a type
  // and an object chosen.
  {
    name: "app-authorization-suggestions",
    path: `${BASE_PATH}/app/authorization/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "carol@example.com",
    apiCall: APP_API_CALL,
    act: async (page) => {
      await page.getByRole("combobox", { name: /^Who/ }).click();
      await page.getByPlaceholder(/Type a name/).fill("erin");
      await page
        .getByRole("option", { name: /Erin Ng/ })
        .waitFor({ timeout: READY_TIMEOUT_MS });
    },
  },
  {
    name: "app-resources-add",
    path: `${BASE_PATH}/app/resources/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "forecast-model",
    apiCall: APP_API_CALL,
    act: async (page) => {
      await page.getByRole("button", { name: "Add resource" }).click();
      await page.getByRole("combobox", { name: /^Type/ }).click();
      await page.getByRole("option", { name: /^SQL warehouse/ }).click();
      const object = page.getByRole("combobox", { name: /^SQL warehouse/ });
      await object.click();
      await page.getByRole("option", { name: "Shared Serverless" }).click();
      await page
        .getByRole("button", { name: "Clear Shared Serverless" })
        .waitFor({ timeout: READY_TIMEOUT_MS });
    },
  },
  // The launcher and the app's Overview, Deployments, Logs and Environment in
  // the worlds of fixtures-f1b.ts.
  {
    name: "launcher-no-apps",
    path: `${BASE_PATH}/`,
    status: 200,
    heading: "Apps",
    dataText: "No apps for you yet",
    apiCall: `${API_PREFIX}/apps`,
    scenario: "no-apps",
  },
  {
    name: "app-overview-node-failed",
    path: `${BASE_PATH}/app/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: NODE_FAILURE_HINT,
    apiCall: APP_API_CALL,
    scenario: "node-release-failed",
  },
  {
    name: "app-deployments-menu",
    path: `${BASE_PATH}/app/deployments/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: NODE_FAILURE_HINT,
    apiCall: APP_API_CALL,
    scenario: "node-release-failed",
    act: openFailedReleaseMenu,
  },
  {
    name: "app-logs-new-lines",
    path: `${BASE_PATH}/app/logs/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    // The toolbar's words: shown once the first poll has answered, with or without lines.
    dataText: "Updates every 2 seconds",
    apiCall: APP_API_CALL,
    scenario: "logs-waiting",
    act: receiveNewLogLines,
  },
  {
    name: "app-logs-never-started",
    path: `${BASE_PATH}/app/logs/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "No output yet",
    apiCall: APP_API_CALL,
    scenario: "logs-never-started",
  },
  {
    name: "app-environment-packages",
    path: `${BASE_PATH}/app/environment/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    // The runtime block stays above both tabs; this world has no Node.js.
    dataText: "Not detected",
    apiCall: APP_API_CALL,
    scenario: "environment-secret",
    act: openPackagesTab,
  },
  {
    name: "app-environment-json",
    path: `${BASE_PATH}/app/environment/?slug=${EXAMPLE_APP_SLUG}`,
    status: 200,
    heading: "Sales dashboard",
    dataText: "OPENAI_API_KEY",
    apiCall: APP_API_CALL,
    scenario: "environment-secret",
    act: showEnvironmentJson,
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
