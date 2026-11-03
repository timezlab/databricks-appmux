/**
 * The end-to-end stand-in for the appmux edge: it serves the built export in
 * `out/` the way the edge will (design 7.3, FR-021) and mocks the admin API.
 *
 *  - The export is mounted under the base path (`/_appmux`), the prefix the
 *    edge reserves; `trailingSlash` pages are directories with an index.html,
 *    a directory without its slash is redirected, an unknown path answers the
 *    export's 404 page with status 404.
 *  - Every response under the prefix carries the reference CSP. The edge will
 *    compute the script hashes from the export's HTML files; here they are
 *    computed per request from the file about to be sent, so a rebuild never
 *    leaves a stale hash. There is no `'unsafe-inline'` and no `'unsafe-eval'`.
 *  - `GET`s of the admin API answer from the contract's example files
 *    (`specs/001-appmux-v1/contracts/examples/`); every write answers the
 *    status of the contract with its example body, or 204. The mock keeps no
 *    state, and cookies and identity headers are ignored.
 *  - A request that carries `X-E2E-Scenario: <name>` is answered from a named
 *    world instead (fixtures.ts): a visitor with a dozen apps, a nearly full
 *    slot, a save that fails. The routes a scenario does not mention answer as
 *    usual.
 *  - Under `/_appmux/__e2e__/` live pages that violate the CSP on purpose. They
 *    are the negative controls: the spec loads them with the same listener as
 *    the real pages, so a listener that saw nothing would show itself.
 *  - Under `/_appmux/__e2e__/primitives/` lives the page of shadcn primitives
 *    that e2e/primitives/build.ts builds (Playwright's global setup), served
 *    like the export and under the same CSP; `control/` below it is its
 *    negative control.
 *
 * Node's `http` module only: this is test tooling, and a dependency would be
 * one more thing to vet.
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";

import { SCENARIO_HEADER, scenarioAnswer } from "./fixtures.ts";

/** Same default as next.config.mjs and tests/support/export-dir.ts. */
export const BASE_PATH = process.env.APPMUX_BASE_PATH ?? "/_appmux";
export const API_PREFIX = `${BASE_PATH}/api/v1`;
export const CONTROL_PREFIX = `${BASE_PATH}/__e2e__`;
export const PRIMITIVES_PREFIX = `${CONTROL_PREFIX}/primitives`;
/** Names the folder e2e/primitives/build.ts built the primitives page into. */
export const PRIMITIVES_DIR_ENV = "APPMUX_E2E_PRIMITIVES_DIR";

const EXPORT_DIR = path.resolve(import.meta.dirname, "../out");
const EXAMPLES_DIR = path.resolve(
  import.meta.dirname,
  "../../specs/001-appmux-v1/contracts/examples",
);

// ------------------------------------------------------------------ the CSP

/** The directives that do not depend on the page (design 7.3, FR-021). */
const FIXED_CSP_DIRECTIVES = [
  "default-src 'none'",
  "style-src 'self'",
  "connect-src 'self'",
  "img-src 'self'",
  "font-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
] as const;

/**
 * The text of every inline `<script>` (one with no `src`) in `html`. The
 * attribute scan steps over quoted values, so a `>` inside one does not end the
 * tag early. The browser hashes the exact text between the tags, which is the
 * raw text of the element (script content is never entity-decoded).
 */
export function inlineScriptBodies(html: string): string[] {
  const scriptElement =
    /<script\b((?:[^>"']|"[^"]*"|'[^']*')*)>([\s\S]*?)<\/script\s*>/gi;
  const bodies: string[] = [];
  for (const match of html.matchAll(scriptElement)) {
    const attributes = match[1] ?? "";
    if (!/(?:^|\s)src\s*=/i.test(attributes)) bodies.push(match[2] ?? "");
  }
  return bodies;
}

/** A CSP source expression for one inline script body. */
export function scriptHashSource(body: string): string {
  return `'sha256-${createHash("sha256").update(body, "utf8").digest("base64")}'`;
}

/** The header value for a page whose inline scripts are `scriptBodies`. */
export function buildCsp(scriptBodies: readonly string[]): string {
  const hashes = [...new Set(scriptBodies.map(scriptHashSource))];
  return [
    ...FIXED_CSP_DIRECTIVES,
    ["script-src 'self'", ...hashes].join(" "),
  ].join("; ");
}

// ----------------------------------------------------------- the mock API

function readExample(name: string): unknown {
  return JSON.parse(
    readFileSync(path.join(EXAMPLES_DIR, `${name}.json`), "utf8"),
  );
}

type JsonObject = Record<string, unknown>;

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readExampleObject(name: string): JsonObject {
  const example = readExample(name);
  if (!isJsonObject(example))
    throw new Error(`${name} example is not an object`);
  return example;
}

interface ApiRoute {
  method: string;
  /** The contract's path with `{param}` segments. */
  pattern: string;
  status: number;
  /** The example file whose body answers; `null` is an empty 204. */
  example: string | null;
}

/** Every route of the contract with the status it answers on success (admin-api.md). */
const API_ROUTES: readonly ApiRoute[] = [
  { method: "GET", pattern: "/me", status: 200, example: "MeResponse" },
  { method: "GET", pattern: "/apps", status: 200, example: "AppList" },
  {
    method: "POST",
    pattern: "/apps/{app}/open-mode",
    status: 204,
    example: null,
  },
  { method: "POST", pattern: "/apps", status: 201, example: "AppDetail" },
  { method: "GET", pattern: "/apps/{app}", status: 200, example: "AppDetail" },
  {
    method: "PATCH",
    pattern: "/apps/{app}",
    status: 200,
    example: "AppDetail",
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/owner",
    status: 200,
    example: "AppDetail",
  },
  { method: "DELETE", pattern: "/apps/{app}", status: 204, example: null },
  {
    method: "POST",
    pattern: "/apps/{app}/start",
    status: 200,
    example: "AppStateResponse",
  },
  {
    method: "POST",
    pattern: "/apps/{app}/stop",
    status: 200,
    example: "AppStateResponse",
  },
  {
    method: "POST",
    pattern: "/apps/{app}/restart",
    status: 200,
    example: "AppStateResponse",
  },
  {
    method: "POST",
    pattern: "/apps/{app}/releases",
    status: 202,
    example: "ReleaseCreated",
  },
  {
    method: "GET",
    pattern: "/apps/{app}/releases/{id}",
    status: 200,
    example: "ReleaseDetail",
  },
  {
    method: "POST",
    pattern: "/apps/{app}/releases/{id}/activate",
    status: 200,
    example: "ReleaseDetail",
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/releases/{id}",
    status: 200,
    example: "ReleaseDetail",
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/releases/{id}/lock",
    status: 200,
    example: "ReleaseDetail",
  },
  {
    method: "DELETE",
    pattern: "/apps/{app}/releases/{id}/lock",
    status: 200,
    example: "ReleaseDetail",
  },
  {
    method: "DELETE",
    pattern: "/apps/{app}/releases/{id}",
    status: 204,
    example: null,
  },
  {
    method: "GET",
    pattern: "/apps/{app}/releases/{id}/source",
    status: 200,
    example: "ReleaseSourceListing",
  },
  {
    method: "GET",
    pattern: "/apps/{app}/releases/{id}/source/{path}",
    status: 200,
    example: "ReleaseSourceFile",
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/config",
    status: 200,
    example: "ConfigChangeResult",
  },
  {
    method: "GET",
    pattern: "/apps/{app}/requirements",
    status: 200,
    example: "RequirementsResponse",
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/resources/{key}",
    status: 200,
    example: "ResourceView",
  },
  {
    method: "DELETE",
    pattern: "/apps/{app}/resources/{key}",
    status: 204,
    example: null,
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/grants",
    status: 200,
    example: "AppGrant",
  },
  {
    method: "DELETE",
    pattern: "/apps/{app}/grants/{type}/{name}",
    status: 204,
    example: null,
  },
  { method: "GET", pattern: "/requests", status: 200, example: "RequestList" },
  { method: "POST", pattern: "/requests", status: 201, example: "RequestRow" },
  {
    method: "POST",
    pattern: "/requests/{id}/withdraw",
    status: 200,
    example: "RequestRow",
  },
  {
    method: "POST",
    pattern: "/requests/{id}/approve",
    status: 200,
    example: "RequestRow",
  },
  {
    method: "POST",
    pattern: "/requests/{id}/deny",
    status: 200,
    example: "RequestRow",
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/delegation/{kind}",
    status: 200,
    example: "AppDetail",
  },
  {
    method: "DELETE",
    pattern: "/apps/{app}/delegation/{kind}",
    status: 200,
    example: "AppDetail",
  },
  { method: "GET", pattern: "/platform", status: 200, example: "PlatformView" },
  {
    method: "PUT",
    pattern: "/platform/settings/{key}",
    status: 200,
    example: "PlatformSetting",
  },
  {
    method: "PUT",
    pattern: "/platform/admins",
    status: 200,
    example: "AdminList",
  },
  {
    method: "PUT",
    pattern: "/platform/egress",
    status: 200,
    example: "EgressRules",
  },
  {
    method: "PUT",
    pattern: "/apps/{app}/egress",
    status: 200,
    example: "AppDetail",
  },
  {
    method: "POST",
    pattern: "/platform/self-check",
    status: 200,
    example: "SelfCheckResult",
  },
  { method: "GET", pattern: "/audit", status: 200, example: "AuditList" },
  {
    method: "GET",
    pattern: "/metrics",
    status: 200,
    example: "MetricsResponse",
  },
  {
    method: "GET",
    pattern: "/principals",
    status: 200,
    example: "PrincipalList",
  },
  {
    method: "GET",
    pattern: "/workspace/browse",
    status: 200,
    example: "WorkspaceListing",
  },
  {
    method: "GET",
    pattern: "/apps/{app}/logs",
    status: 200,
    example: "AppLogs",
  },
  {
    method: "GET",
    pattern: "/apps/{app}/environment",
    status: 200,
    example: "AppEnvironment",
  },
  {
    method: "GET",
    pattern: "/resources/types",
    status: 200,
    example: "ResourceTypeList",
  },
  {
    method: "GET",
    pattern: "/resources/suggestions",
    status: 200,
    example: "ResourceSuggestionList",
  },
  {
    method: "GET",
    pattern: "/metrics/breakdown",
    status: 200,
    example: "MetricsBreakdown",
  },
];

/** `{app}` matches one segment; `{path}` (a source file) spans segments. */
function patternToRegExp(pattern: string): RegExp {
  const source = pattern
    .split("/")
    .map((segment) => {
      if (segment === "{path}") return "(?<path>.+)";
      const parameter = /^\{(\w+)\}$/.exec(segment);
      return parameter ? `(?<${parameter[1]}>[^/]+)` : segment;
    })
    .join("/");
  return new RegExp(`^${source}$`);
}

const COMPILED_ROUTES = API_ROUTES.map((route) => ({
  route,
  matcher: patternToRegExp(route.pattern),
}));

/** The contract's error shape (admin-api.md, ErrorBody). */
function errorBody(code: string, message: string): JsonObject {
  return { error: { code, message } };
}

const MINUTE_MS = 60_000;
const MAX_WINDOW_MINUTES = 1440;
const DEFAULT_WINDOW_MINUTES = 60;
const PLATFORM_APP_SLUG = "_platform";

function wholeSeconds(time: Date): string {
  return time.toISOString().replace(/\.\d+Z$/, "Z");
}

function scaled(value: unknown, factor: number): unknown {
  return typeof value === "number" ? Math.round(value * factor) : value;
}

/** Fields that move with traffic; the rest of an example row is kept as it is. */
const TRAFFIC_FIELDS = [
  "r2xx",
  "r3xx",
  "r4xx",
  "r5xx",
  "upstream_errors",
  "limited",
  "bytes_in",
  "bytes_out",
  "in_flight_max",
  "rss_mb_max",
  "egress_bytes_out",
  "egress_bytes_in",
  "egress_dropped",
  "mem_used_mb",
] as const;

/**
 * One row per minute and series, shaped like the contract's example rows
 * (the `sales` row for an app, the `_platform` row for the platform), with
 * traffic that rises and falls so a chart has something to draw.
 */
function metricsFor(
  query: URLSearchParams,
  appSlugs: readonly string[],
): JsonObject {
  const example = readExampleObject("MetricsResponse");
  const rows = Array.isArray(example.items)
    ? example.items.filter(isJsonObject)
    : [];
  const appTemplate = rows.find((row) => row.app_slug !== PLATFORM_APP_SLUG);
  const platformTemplate = rows.find(
    (row) => row.app_slug === PLATFORM_APP_SLUG,
  );
  if (!appTemplate || !platformTemplate) {
    throw new Error(
      "the MetricsResponse example needs an app row and a platform row",
    );
  }

  const toParam = query.get("to");
  const fromParam = query.get("from");
  const to = new Date(toParam ?? Date.now());
  const from = new Date(
    fromParam ?? to.getTime() - DEFAULT_WINDOW_MINUTES * MINUTE_MS,
  );
  const requested = query.get("app");
  const series = requested ? [requested] : [...appSlugs, PLATFORM_APP_SLUG];

  const minutes = Math.floor((to.getTime() - from.getTime()) / MINUTE_MS);
  if (Number.isNaN(minutes) || minutes < 0 || minutes > MAX_WINDOW_MINUTES) {
    throw new RangeError("the window is over 1440 minutes or not a window");
  }

  const items: JsonObject[] = [];
  for (const [seriesIndex, slug] of series.entries()) {
    const template =
      slug === PLATFORM_APP_SLUG ? platformTemplate : appTemplate;
    for (let step = 0; step < minutes; step += 1) {
      const factor = 0.65 + 0.35 * Math.sin(step / 6 + seriesIndex * 1.7);
      const row: JsonObject = {
        ...template,
        app_slug: slug,
        minute: wholeSeconds(new Date(from.getTime() + step * MINUTE_MS)),
      };
      for (const field of TRAFFIC_FIELDS)
        row[field] = scaled(template[field], factor);
      const latencies = Array.isArray(template.lat_hist)
        ? template.lat_hist
        : [];
      row.lat_hist = latencies.map((count) => scaled(count, factor));
      row.requests = ["r2xx", "r3xx", "r4xx", "r5xx"].reduce(
        (sum, field) => sum + Number(row[field]),
        0,
      );
      items.push(row);
    }
  }
  return {
    from: wholeSeconds(from),
    to: wholeSeconds(to),
    truncated: false,
    items,
  };
}

/** The slugs and display names the AppList example holds. */
function exampleApps(): JsonObject[] {
  const items = readExampleObject("AppList").items;
  return Array.isArray(items) ? items.filter(isJsonObject) : [];
}

/** The example body for a route, adapted to the path where the example would lie (another slug, another release). */
function exampleBody(
  route: ApiRoute,
  params: Record<string, string | undefined>,
  query: URLSearchParams,
): { status: number; body: unknown } {
  if (route.pattern === "/metrics") {
    const slugs = exampleApps().map((app) => String(app.slug));
    try {
      return { status: 200, body: metricsFor(query, slugs) };
    } catch (cause) {
      if (cause instanceof RangeError) {
        return { status: 400, body: errorBody("bad_request", cause.message) };
      }
      throw cause;
    }
  }
  if (route.example === null) return { status: route.status, body: undefined };

  const example = readExample(route.example);
  const slug = params.app;
  if (
    slug !== undefined &&
    isJsonObject(example) &&
    route.example === "AppDetail"
  ) {
    const known = exampleApps().find((app) => app.slug === slug);
    if (route.method === "GET" && !known && slug !== example.slug) {
      return { status: 404, body: errorBody("not_found", `no app '${slug}'`) };
    }
    return {
      status: route.status,
      body: {
        ...example,
        slug,
        display_name: known?.display_name ?? example.display_name,
        state: known?.state ?? example.state,
      },
    };
  }
  if (
    params.id !== undefined &&
    isJsonObject(example) &&
    route.example === "ReleaseDetail"
  ) {
    return {
      status: route.status,
      body: { ...example, release_no: Number(params.id) },
    };
  }
  return { status: route.status, body: example };
}

export interface ApiCall {
  method: string;
  /** Path and query, as the browser sent them. */
  url: string;
  status: number;
}

// -------------------------------------------------- the negative controls

/**
 * Tiny pages that break the CSP on purpose. Each is served with a CSP
 * computed from `policyHtml`, the page as it should have been, so the
 * difference between it and `html` is exactly what the browser must refuse.
 */
interface ControlPage {
  html: string;
  policyHtml: string;
}

const COVERED_SCRIPT = "window.__e2eCoveredScriptRan = true;";
const UNCOVERED_SCRIPT = "window.__e2eUncoveredScriptRan = true;";

function controlShell(heading: string, body: string): string {
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>${heading}</title></head><body><main><h1>${heading}</h1>${body}</main></body></html>`;
}

const CONTROL_PAGES: Record<string, ControlPage> = {
  "inline-style": {
    html: controlShell(
      "Control: inline style",
      '<p id="probe" style="background-color: rgb(255, 0, 0)">An inline style attribute</p><style>#probe { border: 4px solid rgb(0, 0, 255); }</style>',
    ),
    policyHtml: controlShell("Control: inline style", '<p id="probe">None</p>'),
  },
  "inline-script": {
    // One script whose hash the policy lists (it must run: this proves the
    // hashes are computed the way the browser computes them) and one it does not.
    html: controlShell(
      "Control: inline script",
      `<script>${COVERED_SCRIPT}</script><script>${UNCOVERED_SCRIPT}</script>`,
    ),
    policyHtml: controlShell(
      "Control: inline script",
      `<script>${COVERED_SCRIPT}</script>`,
    ),
  },
};

/** Not a CSP control: a page that is wider than any phone, to prove the spec's overflow check can fail. */
CONTROL_PAGES.overflow = {
  html: controlShell(
    "Control: overflow",
    `<p id="probe">${"x".repeat(3000)}</p>`,
  ),
  policyHtml: controlShell("Control: overflow", ""),
};

/** The route that serves the real launcher with its first script hash left out of the policy. */
const MISSING_HASH_CONTROL = "missing-hash";

// ------------------------------------------------------- static serving

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
};
const FALLBACK_CONTENT_TYPE = "application/octet-stream";

/** Headers of every response under the prefix (edge-http.md, "Admin UI responses"). */
function securityHeaders(csp: string): Record<string, string> {
  return {
    "Content-Security-Policy": csp,
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
  };
}

function reply(
  request: IncomingMessage,
  response: ServerResponse,
  status: number,
  headers: Record<string, string>,
  body: string | Buffer,
): void {
  response.writeHead(status, {
    ...headers,
    "Content-Length": Buffer.byteLength(body),
  });
  response.end(request.method === "HEAD" ? undefined : body);
}

function replyJson(
  request: IncomingMessage,
  response: ServerResponse,
  status: number,
  body: unknown,
): void {
  const headers = {
    ...securityHeaders(buildCsp([])),
    "Content-Type": "application/json; charset=utf-8",
  };
  if (body === undefined || status === 204) {
    response.writeHead(status, headers);
    response.end();
    return;
  }
  reply(request, response, status, headers, JSON.stringify(body));
}

function replyText(
  request: IncomingMessage,
  response: ServerResponse,
  status: number,
  text: string,
): void {
  reply(
    request,
    response,
    status,
    {
      ...securityHeaders(buildCsp([])),
      "Content-Type": "text/plain; charset=utf-8",
    },
    text,
  );
}

function replyHtml(
  request: IncomingMessage,
  response: ServerResponse,
  status: number,
  html: string,
  policyHtml: string = html,
): void {
  reply(
    request,
    response,
    status,
    {
      ...securityHeaders(buildCsp(inlineScriptBodies(policyHtml))),
      "Content-Type": CONTENT_TYPES[".html"] ?? FALLBACK_CONTENT_TYPE,
    },
    html,
  );
}

/** What a request path under a site's prefix names in its folder, or why it names nothing. */
type Resolved =
  | { kind: "file"; file: string }
  | { kind: "redirect"; location: string }
  | { kind: "missing" }
  | { kind: "bad-request" };

async function isDirectory(file: string): Promise<boolean> {
  try {
    return (await stat(file)).isDirectory();
  } catch {
    // No such entry: the caller treats it as a missing file.
    return false;
  }
}

async function isFile(file: string): Promise<boolean> {
  try {
    return (await stat(file)).isFile();
  } catch {
    // No such entry: the caller treats it as a missing file.
    return false;
  }
}

/** A folder served under a path prefix: the export, or the primitives page. */
interface StaticSite {
  root: string;
  prefix: string;
}

async function resolveSitePath(
  site: StaticSite,
  pathname: string,
  search: string,
): Promise<Resolved> {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname.slice(site.prefix.length));
  } catch {
    return { kind: "bad-request" };
  }
  // The edge refuses ambiguous paths; so does this stand-in (FR-017).
  const segments = decoded.split("/").slice(1);
  const ambiguous =
    decoded.includes("\0") ||
    decoded.includes("\\") ||
    segments.slice(0, -1).includes("") ||
    segments.some((segment) => segment === ".." || segment === ".");
  if (ambiguous) return { kind: "bad-request" };

  const target = path.resolve(site.root, ...segments);
  if (target !== site.root && !target.startsWith(site.root + path.sep)) {
    return { kind: "bad-request" };
  }
  if (await isDirectory(target)) {
    if (!pathname.endsWith("/")) {
      return { kind: "redirect", location: `${pathname}/${search}` };
    }
    const index = path.join(target, "index.html");
    return (await isFile(index))
      ? { kind: "file", file: index }
      : { kind: "missing" };
  }
  return (await isFile(target))
    ? { kind: "file", file: target }
    : { kind: "missing" };
}

/** Answers a GET under `site.prefix`; a missing file answers `replyMissing`. */
async function serveSite(
  request: IncomingMessage,
  response: ServerResponse,
  site: StaticSite,
  pathname: string,
  search: string,
  replyMissing: () => Promise<void>,
): Promise<void> {
  const resolved = await resolveSitePath(site, pathname, search);
  switch (resolved.kind) {
    case "bad-request":
      replyText(request, response, 400, "bad request");
      return;
    case "redirect":
      response.writeHead(308, {
        ...securityHeaders(buildCsp([])),
        Location: resolved.location,
      });
      response.end();
      return;
    case "missing":
      await replyMissing();
      return;
    case "file": {
      const extension = path.extname(resolved.file);
      if (extension === ".html") {
        replyHtml(
          request,
          response,
          200,
          await readFile(resolved.file, "utf8"),
        );
        return;
      }
      reply(
        request,
        response,
        200,
        {
          ...securityHeaders(buildCsp([])),
          "Content-Type": CONTENT_TYPES[extension] ?? FALLBACK_CONTENT_TYPE,
        },
        await readFile(resolved.file),
      );
      return;
    }
  }
}

async function serveExport(
  request: IncomingMessage,
  response: ServerResponse,
  pathname: string,
  search: string,
): Promise<void> {
  await serveSite(
    request,
    response,
    { root: EXPORT_DIR, prefix: BASE_PATH },
    pathname,
    search,
    async () => {
      const notFound = path.join(EXPORT_DIR, "404.html");
      replyHtml(request, response, 404, await readFile(notFound, "utf8"));
    },
  );
}

async function servePrimitives(
  request: IncomingMessage,
  response: ServerResponse,
  pathname: string,
  search: string,
): Promise<void> {
  const root = process.env[PRIMITIVES_DIR_ENV];
  if (root === undefined) {
    replyText(
      request,
      response,
      404,
      "no primitives page: Playwright's global setup (e2e/primitives/build.ts) builds it",
    );
    return;
  }
  await serveSite(
    request,
    response,
    { root, prefix: PRIMITIVES_PREFIX },
    pathname,
    search,
    () => {
      replyText(request, response, 404, "not found");
      return Promise.resolve();
    },
  );
}

async function serveControl(
  request: IncomingMessage,
  response: ServerResponse,
  name: string,
): Promise<void> {
  const page = CONTROL_PAGES[name];
  if (page) {
    replyHtml(request, response, 200, page.html, page.policyHtml);
    return;
  }
  if (name === MISSING_HASH_CONTROL) {
    const launcher = await readFile(
      path.join(EXPORT_DIR, "index.html"),
      "utf8",
    );
    const bodies = inlineScriptBodies(launcher);
    reply(
      request,
      response,
      200,
      {
        ...securityHeaders(buildCsp(bodies.slice(1))),
        "Content-Type": CONTENT_TYPES[".html"] ?? FALLBACK_CONTENT_TYPE,
      },
      launcher,
    );
    return;
  }
  replyText(request, response, 404, "no such control");
}

function handleApi(
  request: IncomingMessage,
  response: ServerResponse,
  pathname: string,
  query: URLSearchParams,
  calls: ApiCall[],
): void {
  const apiPath = pathname.slice(API_PREFIX.length);
  const method = request.method ?? "GET";
  const record = (status: number) =>
    calls.push({
      method,
      url: `${pathname}${query.size > 0 ? `?${query}` : ""}`,
      status,
    });
  let decodedPath: string;
  try {
    decodedPath = decodeURIComponent(apiPath);
  } catch {
    record(400);
    replyJson(
      request,
      response,
      400,
      errorBody("bad_request", "bad path encoding"),
    );
    return;
  }
  for (const { route, matcher } of COMPILED_ROUTES) {
    if (
      route.method !== method &&
      !(method === "HEAD" && route.method === "GET")
    )
      continue;
    const match = matcher.exec(decodedPath);
    if (!match) continue;
    const params = { ...match.groups };
    const scenario = request.headers[SCENARIO_HEADER];
    const scenarioName = Array.isArray(scenario) ? scenario[0] : scenario;
    const { status, body } =
      (scenarioName === undefined
        ? null
        : scenarioAnswer(
            scenarioName,
            route.method,
            route.pattern,
            params,
            readExample,
          )) ?? exampleBody(route, params, query);
    record(status);
    replyJson(request, response, status, body);
    return;
  }
  record(404);
  replyJson(
    request,
    response,
    404,
    errorBody("not_found", "no such route in the e2e mock"),
  );
}

// -------------------------------------------------------------- the server

export interface E2eServer {
  /** `http://127.0.0.1:<port>`: the port is the kernel's pick, so runs never collide. */
  origin: string;
  /** Every API call the pages made, in order. */
  apiCalls: readonly ApiCall[];
  close: () => Promise<void>;
}

export async function startServer(): Promise<E2eServer> {
  const calls: ApiCall[] = [];
  const server = createServer((request, response) => {
    void (async () => {
      const method = request.method ?? "GET";
      const url = new URL(request.url ?? "/", "http://localhost");
      const { pathname } = url;
      try {
        if (pathname === BASE_PATH) {
          response.writeHead(308, { Location: `${BASE_PATH}/${url.search}` });
          response.end();
        } else if (!pathname.startsWith(`${BASE_PATH}/`)) {
          // The edge would route this to a child; the stand-in hosts none.
          replyText(request, response, 404, "not found");
        } else if (pathname.startsWith(`${API_PREFIX}/`)) {
          handleApi(request, response, pathname, url.searchParams, calls);
        } else if (method !== "GET" && method !== "HEAD") {
          replyText(request, response, 405, "method not allowed");
        } else if (
          pathname === PRIMITIVES_PREFIX ||
          pathname.startsWith(`${PRIMITIVES_PREFIX}/`)
        ) {
          await servePrimitives(request, response, pathname, url.search);
        } else if (pathname.startsWith(`${CONTROL_PREFIX}/`)) {
          await serveControl(
            request,
            response,
            pathname.slice(CONTROL_PREFIX.length + 1).replace(/\/$/, ""),
          );
        } else {
          await serveExport(request, response, pathname, url.search);
        }
      } catch (cause) {
        // Never reach the browser with detail (security rule); the runner sees it.
        console.error("e2e server error:", cause);
        if (!response.headersSent)
          replyText(request, response, 500, "internal error");
        else response.destroy();
      }
    })();
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address() as AddressInfo;
  return {
    origin: `http://127.0.0.1:${port}`,
    apiCalls: calls,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
        server.closeAllConnections();
      }),
  };
}
