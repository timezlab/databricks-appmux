/**
 * The people and the data the screenshots need beyond the contract's examples.
 *
 * The mock API answers from the contract's example files, which describe one
 * platform admin looking at three apps. Some screens only make sense for someone
 * else (a visitor who may open a dozen apps and manage none), or for other
 * figures (a slot that is nearly full), or after a failure. A page asks for such
 * a world by sending `X-E2E-Scenario: <name>`; the server then lets
 * `scenarioAnswer` replace the answer of the routes below and answers every
 * other route as usual.
 *
 * The scenarios are data for looking at a screen. They are not the contract:
 * each body is built from the contract's example, so it still parses with the
 * UI's own schemas.
 */
import { F4_SCENARIOS, f4ScenarioAnswer, isF4Scenario } from "./fixtures-f4.ts";

import {
  F1B_SCENARIOS,
  f1bScenarioAnswer,
  isF1bScenario,
} from "./fixtures-f1b.ts";

export const SCENARIO_HEADER = "x-e2e-scenario";

export const SCENARIOS = [
  "visitor",
  "requester",
  "memory-low",
  "memory-warning",
  "memory-danger",
  "settings-one-fails",
  ...F1B_SCENARIOS,
  ...F4_SCENARIOS,
] as const;
export type Scenario = (typeof SCENARIOS)[number];

export interface ScenarioAnswer {
  status: number;
  body: unknown;
}

type ReadExample = (name: string) => unknown;
type JsonObject = Record<string, unknown>;

function isScenario(name: string): name is Scenario {
  return (SCENARIOS as readonly string[]).includes(name);
}

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asObject(value: unknown, what: string): JsonObject {
  if (!isJsonObject(value)) throw new Error(`${what} is not an object`);
  return value;
}

/** The person who is looking: someone the examples do not mention, so no page can show them as an owner or a requester by accident. */
const VISITOR_EMAIL = "erin@example.com";

function visitorMe(readExample: ReadExample): JsonObject {
  return {
    ...asObject(readExample("MeResponse"), "MeResponse"),
    email: VISITOR_EMAIL,
    groups: ["analysts"],
    is_platform_admin: false,
  };
}

type AppSeed = readonly [
  slug: string,
  displayName: string,
  state: "running" | "stopped" | "starting" | "crashed",
  permission?: "CAN_USE" | "CAN_MANAGE",
];

function appList(seeds: readonly AppSeed[]): JsonObject {
  return {
    items: seeds.map(([slug, displayName, state, permission]) => ({
      slug,
      display_name: displayName,
      state,
      permission: permission ?? "CAN_USE",
      open_modes: [],
    })),
  };
}

/** About a dozen apps, in no particular order: the list is sorted by the page, not by the server. */
const VISITOR_APPS: readonly AppSeed[] = [
  ["sales-forecast", "Sales forecast", "running"],
  ["hr-onboarding", "HR onboarding", "running"],
  ["data-quality", "Data quality monitor", "running"],
  ["wiki", "Team wiki", "running"],
  ["finance-close", "Finance close checklist", "stopped"],
  ["churn-explorer", "Churn explorer", "running"],
  ["app-10", "App 10", "running"],
  ["app-2", "App 2", "running"],
  ["support-triage", "Support triage board", "starting"],
  ["campaign-roi", "Campaign ROI", "running"],
  ["inventory", "Inventory levels", "running"],
  ["okr-tracker", "OKR tracker", "running"],
];

const REQUESTER_APPS: readonly AppSeed[] = [
  ["wiki", "Team wiki", "running"],
  ["sales-forecast", "Sales forecast", "running"],
  ["finance-close", "Finance close checklist", "stopped"],
  ["churn-explorer", "Churn explorer", "stopped", "CAN_MANAGE"],
  ["okr-tracker", "OKR tracker", "running", "CAN_MANAGE"],
];

function requestRow(fields: JsonObject): JsonObject {
  return {
    closed_by: null,
    closed_at: null,
    closed_note: null,
    current: null,
    requires_reason: true,
    app_version: null,
    ...fields,
  };
}

/** The visitor's own requests: one waiting, one approved, one denied with a note, one withdrawn. */
function requesterRequests(): JsonObject {
  return {
    items: [
      requestRow({
        request_id: 21,
        app_slug: null,
        kind: "register",
        payload: {
          slug: "ops-board",
          display_name: "Ops board",
          source_path: "/Workspace/Shared/apps/ops-board",
        },
        reason: "a shared board for the operations team",
        requested_by: VISITOR_EMAIL,
        requested_at: "2026-10-28T09:00:00Z",
        status: "denied",
        closed_by: "dana@example.com",
        closed_at: "2026-10-28T11:30:00Z",
        closed_note: "The source folder is empty. Add the app, then ask again.",
      }),
      requestRow({
        request_id: 22,
        app_slug: "churn-explorer",
        kind: "settings",
        payload: { memory_budget_mb: 384 },
        current: { memory_budget_mb: 256 },
        reason: "the new model needs more memory",
        requested_by: VISITOR_EMAIL,
        requested_at: "2026-10-29T14:10:00Z",
        status: "approved",
        closed_by: "dana@example.com",
        closed_at: "2026-10-29T15:00:00Z",
      }),
      requestRow({
        request_id: 23,
        app_slug: "okr-tracker",
        kind: "delegation",
        payload: { obo: true },
        current: { obo: false },
        reason: "show each reader their own workspace files",
        requested_by: VISITOR_EMAIL,
        requested_at: "2026-10-30T08:15:00Z",
        status: "withdrawn",
        closed_by: VISITOR_EMAIL,
        closed_at: "2026-10-30T08:40:00Z",
      }),
      wikiRequest(),
    ],
  };
}

/** The visitor's one pending request; `fields` close it (the answer of Withdraw). */
function wikiRequest(fields: JsonObject = {}): JsonObject {
  return requestRow({
    request_id: 24,
    app_slug: null,
    kind: "register",
    payload: {
      slug: "wiki-2",
      display_name: "Team wiki 2",
      source_path: "/Workspace/Shared/apps/wiki-2",
    },
    reason: "the second team wiki, a small Flask app",
    requested_by: VISITOR_EMAIL,
    requested_at: "2026-10-30T09:55:00Z",
    status: "pending",
    ...fields,
  });
}

/** The platform view with the slot's memory set to `committedMb`; the other figures follow from the example. */
function platformWithCommitted(
  readExample: ReadExample,
  committedMb: number,
): JsonObject {
  const view = asObject(readExample("PlatformView"), "PlatformView");
  const memory = asObject(view.memory, "PlatformView.memory");
  const limit = Number(memory.limit_mb);
  const reserve = Number(memory.reserve_mb);
  return {
    ...view,
    memory: {
      ...memory,
      committed_mb: committedMb,
      free_mb: limit - reserve - committedMb,
    },
  };
}

/** Committed memory of the three memory scenarios, in MB of the 7,168 the example slot offers apps: 25 %, 86 %, 98 %. */
const COMMITTED_MB = {
  "memory-low": 1800,
  "memory-warning": 6200,
  "memory-danger": 7000,
} as const;

/** The setting whose save fails in `settings-one-fails`. */
export const FAILING_SETTING_KEY = "max_request_body_mb";

/**
 * The answer a scenario gives to a route, or `null` when it leaves the route to
 * the examples. `pattern` is the contract's path (`/apps/{app}`); `params`
 * holds its `{...}` segments.
 */
export function scenarioAnswer(
  scenario: string,
  method: string,
  pattern: string,
  params: Readonly<Record<string, string | undefined>>,
  readExample: ReadExample,
): ScenarioAnswer | null {
  if (!isScenario(scenario)) {
    // A typo in a page entry must not quietly show the default world.
    throw new Error(
      `unknown e2e scenario "${scenario}"; known: ${SCENARIOS.join(", ")}`,
    );
  }
  const route = `${method} ${pattern}`;
  if (isF1bScenario(scenario)) {
    return f1bScenarioAnswer(scenario, route, params, readExample);
  }
  if (isF4Scenario(scenario))
    return f4ScenarioAnswer(scenario, route, readExample);
  switch (scenario) {
    case "visitor":
      if (route === "GET /me") {
        return { status: 200, body: visitorMe(readExample) };
      }
      if (route === "GET /apps") {
        return { status: 200, body: appList(VISITOR_APPS) };
      }
      if (route === "GET /requests") {
        return { status: 200, body: { items: [] } };
      }
      return null;
    case "requester":
      if (route === "GET /me") {
        return { status: 200, body: visitorMe(readExample) };
      }
      if (route === "GET /apps") {
        return { status: 200, body: appList(REQUESTER_APPS) };
      }
      if (route === "GET /requests") {
        return { status: 200, body: requesterRequests() };
      }
      if (route === "POST /requests/{id}/withdraw") {
        const closed = {
          status: "withdrawn",
          closed_by: VISITOR_EMAIL,
          closed_at: "2026-10-30T10:05:00Z",
        };
        return { status: 200, body: wikiRequest(closed) };
      }
      return null;
    case "memory-low":
    case "memory-warning":
    case "memory-danger":
      return route === "GET /platform"
        ? {
            status: 200,
            body: platformWithCommitted(readExample, COMMITTED_MB[scenario]),
          }
        : null;
    case "settings-one-fails":
      if (
        route === "PUT /platform/settings/{key}" &&
        params.key === FAILING_SETTING_KEY
      ) {
        return {
          status: 503,
          body: {
            error: {
              code: "unavailable",
              message: "The settings store did not answer. Try again.",
            },
          },
        };
      }
      return null;
  }
}
