/**
 * The worlds of the launcher and of the app's Overview, Deployments, Logs and
 * Environment that the contract's examples do not show (lane F1b). Registered
 * in `fixtures.ts`, which sends a scenario named here to `f1bScenarioAnswer`.
 *
 * As there, each body is built from the contract's example, so it still parses
 * with the UI's own schemas; the figures stand in for what only a real child
 * shows (its output, a Node.js start in a small memory budget).
 */
import type { ScenarioAnswer } from "./fixtures.ts";

export const F1B_SCENARIOS = [
  "no-apps",
  "node-release-failed",
  "logs-waiting",
  "logs-new-lines",
  "logs-never-started",
  "environment-secret",
] as const;
export type F1bScenario = (typeof F1B_SCENARIOS)[number];

export function isF1bScenario(name: string): name is F1bScenario {
  return (F1B_SCENARIOS as readonly string[]).includes(name);
}

type ReadExample = (name: string) => unknown;
type JsonObject = Record<string, unknown>;

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asObject(value: unknown, what: string): JsonObject {
  if (!isJsonObject(value)) throw new Error(`${what} is not an object`);
  return value;
}

function ok(body: unknown): ScenarioAnswer {
  return { status: 200, body };
}

// ------------------------------------------------ a person with no app yet

/** Someone the examples do not mention, so no page shows them as an owner or a requester. */
function personWithNoAppMe(readExample: ReadExample): JsonObject {
  return {
    ...asObject(readExample("MeResponse"), "MeResponse"),
    email: "frank@example.com",
    groups: [],
    is_platform_admin: false,
  };
}

// --------------------------- a Node.js release that failed in a 64 MB budget

/** The failed target of the example (`AppDetail.releases`, release 8). */
const NODE_RELEASE_NO = 8;
const NODE_BUDGET_MB = 64;

/** How the start ended, as the reconciler writes it (`start_failure.start_exit_reason`). */
const NODE_START_REASON = "the app was stopped by signal SIGTRAP after 0.4 s";

/** The sentence `start_failure.failure_hint` gives for this end, word for word. */
export const NODE_FAILURE_HINT =
  "The memory budget is below what Node.js needs: the sandbox limits a child's address space to 2 times its memory budget, and Node.js needs a budget of about 768 MB, so raise the app's memory budget.";

/** What V8 prints when it cannot reserve its code range under the address-space limit. */
const NODE_START_OUTPUT = [
  "> sales-dashboard@1.4.0 start",
  "> node server.js",
  "",
  "#",
  "# Fatal process out of memory: Failed to reserve virtual memory for CodeRange",
  "#",
  "----- Native stack trace -----",
  "",
  " 1: 0xe1a0c1 node::DumpNativeBacktrace(_IO_FILE*) [node]",
  " 2: 0xf0b0a2 V8_Fatal(char const*, ...) [node]",
  " 3: 0x1159c3e v8::internal::CodeRange::InitReservation [node]",
].join("\n");

/** The example app, now a Node.js app in a 64 MB budget whose target release failed at start. */
function nodeAppDetail(readExample: ReadExample, slug: string): JsonObject {
  const app = asObject(readExample("AppDetail"), "AppDetail");
  const releases = Array.isArray(app.releases) ? app.releases : [];
  return {
    ...app,
    slug,
    runtime: "node",
    memory_budget_mb: NODE_BUDGET_MB,
    releases: releases.map((item: unknown) => {
      const release = asObject(item, "AppDetail.releases[]");
      return release.release_no === NODE_RELEASE_NO
        ? { ...release, status_reason: NODE_START_REASON }
        : release;
    }),
  };
}

function nodeFailedRelease(readExample: ReadExample): JsonObject {
  const release = asObject(readExample("ReleaseDetail"), "ReleaseDetail");
  const detected = asObject(release.detected, "ReleaseDetail.detected");
  return {
    ...release,
    release_no: NODE_RELEASE_NO,
    status: "failed",
    status_reason: NODE_START_REASON,
    status_at: "2026-10-30T09:31:00Z",
    name: null,
    tags: [],
    is_target: true,
    created_at: "2026-10-30T09:30:00Z",
    build_log_tail: NODE_START_OUTPUT,
    failure_hint: NODE_FAILURE_HINT,
    detected: {
      ...detected,
      runtime: "node",
      framework: "express",
      command: ["npm", "run", "start"],
    },
    wheels: [],
  };
}

// ------------------------------------------------------- the app's output

/** One start of the child: the lines below belong to it, so the view keeps them across polls. */
const LOG_INSTANCE = "b7e1c2d3a4f50617";

function logsAnswer(fields: JsonObject): JsonObject {
  return {
    state: "running",
    instance: LOG_INSTANCE,
    lines: [],
    next_after: 0,
    dropped: 0,
    last_exit: null,
    ...fields,
  };
}

type LogSeed = readonly [
  stream: "stdout" | "stderr" | "appmux",
  second: number,
  text: string,
];

/** A uvicorn start, as the child prints it after appmux starts it. */
const NEW_LOG_LINES: readonly LogSeed[] = [
  ["appmux", 0, "started"],
  ["stderr", 1, "INFO:     Started server process [42]"],
  ["stderr", 1, "INFO:     Waiting for application startup."],
  ["stdout", 2, "loaded 1284 rows from the forecast table"],
  ["stderr", 2, "INFO:     Application startup complete."],
  [
    "stderr",
    2,
    "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)",
  ],
  ["stderr", 5, 'INFO:     127.0.0.1:51234 - "GET / HTTP/1.1" 200 OK'],
  ["stdout", 6, "refreshed the cache in 0.8 s"],
];

/** The text the e2e page waits for once the poll after the switch has answered. */
export const NEW_LOG_LINE_TEXT = "INFO:     Application startup complete.";

function newLogLines(): JsonObject {
  const lines = NEW_LOG_LINES.map(([stream, second, text], index) => ({
    seq: index + 1,
    at: `2026-11-03T09:14:0${String(second)}Z`,
    stream,
    text,
  }));
  return logsAnswer({ lines, next_after: lines.length });
}

// ------------------------------------------- an environment with secrets

/**
 * The active release's environment with two secrets (never a value), a
 * resource key the app does not have yet (`null`, not a secret), a requirement
 * written without a version, and no Node.js.
 */
function environmentWithSecrets(): JsonObject {
  const variable = (
    name: string,
    value: string | null,
    source: "config" | "resource" | "platform",
    resourceKey: string | null = null,
    isSecret = false,
  ): JsonObject => ({
    name,
    value,
    source,
    resource_key: resourceKey,
    is_secret: isSecret,
  });
  return {
    release: 7,
    runtime: { kind: "python", python_version: "3.11.9", node_version: null },
    variables: [
      variable("LOG_LEVEL", "info", "config"),
      variable("WAREHOUSE", "a1b2c3d4e5f67890", "resource", "sql-warehouse"),
      variable("OPENAI_API_KEY", null, "resource", "openai-api-key", true),
      variable(
        "DATABRICKS_CLIENT_SECRET",
        null,
        "resource",
        "sp-client-secret",
        true,
      ),
      variable("FORECAST_ENDPOINT", null, "resource", "forecast-model"),
      variable("APPMUX_BASE_PATH", "/sales", "platform"),
      variable("DATABRICKS_APP_PORT", "8000", "platform"),
    ],
    packages: [
      { name: "fastapi", version: "==0.115.0", source: "release" },
      { name: "uvicorn[standard]", version: ">=0.30", source: "release" },
      { name: "pandas", version: null, source: "release" },
      { name: "fastapi", version: "0.115.0", source: "wheel" },
      { name: "acme-tools", version: "1.2.0", source: "wheel" },
      { name: "pydantic", version: "2.11.7", source: "platform" },
      { name: "databricks-sdk", version: "0.67.0", source: "platform" },
    ],
  };
}

/**
 * The answer an F1b scenario gives to a route (`GET /apps/{app}`), or `null`
 * when it leaves the route to the examples.
 */
export function f1bScenarioAnswer(
  scenario: F1bScenario,
  route: string,
  params: Readonly<Record<string, string | undefined>>,
  readExample: ReadExample,
): ScenarioAnswer | null {
  switch (scenario) {
    case "no-apps":
      if (route === "GET /me") return ok(personWithNoAppMe(readExample));
      if (route === "GET /apps" || route === "GET /requests") {
        return ok({ items: [] });
      }
      return null;
    case "node-release-failed":
      if (route === "GET /apps/{app}" && params.app !== undefined) {
        return ok(nodeAppDetail(readExample, params.app));
      }
      if (
        route === "GET /apps/{app}/releases/{id}" &&
        params.id === String(NODE_RELEASE_NO)
      ) {
        return ok(nodeFailedRelease(readExample));
      }
      return null;
    case "logs-waiting":
      // Started, nothing printed yet: the page shows its empty state until a
      // later poll (after the page switches to `logs-new-lines`) has lines.
      return route === "GET /apps/{app}/logs"
        ? ok(logsAnswer({ state: "starting" }))
        : null;
    case "logs-new-lines":
      // The mock ignores `after`, so every poll repeats these lines; the view
      // keeps one copy of each, since it merges by `seq` within one instance.
      return route === "GET /apps/{app}/logs" ? ok(newLogLines()) : null;
    case "logs-never-started":
      return route === "GET /apps/{app}/logs"
        ? ok(logsAnswer({ state: "never_started", instance: null }))
        : null;
    case "environment-secret":
      return route === "GET /apps/{app}/environment"
        ? ok(environmentWithSecrets())
        : null;
  }
}
