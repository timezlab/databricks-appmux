/** The routes of the admin API and how a route string becomes a URL. No `fetch` here, so this file is testable alone. */

// `next.config.mjs` injects the build-time base path; the fallback serves the
// unit tests, which run outside Next.
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "/_appmux";

export const API_BASE_URL = `${BASE_PATH}/api/v1`;

/** Every route of the contract, as `METHOD /path/{param}` (no query string). */
export const API_ROUTES = [
  "GET /me",
  "GET /apps",
  "POST /apps/{app}/open-mode",
  "POST /apps",
  "GET /apps/{app}",
  "PATCH /apps/{app}",
  "PUT /apps/{app}/owner",
  "DELETE /apps/{app}",
  "POST /apps/{app}/start",
  "POST /apps/{app}/stop",
  "POST /apps/{app}/restart",
  "POST /apps/{app}/releases",
  "GET /apps/{app}/releases/{id}",
  "POST /apps/{app}/releases/{id}/activate",
  "PUT /apps/{app}/releases/{id}",
  "PUT /apps/{app}/releases/{id}/lock",
  "DELETE /apps/{app}/releases/{id}/lock",
  "DELETE /apps/{app}/releases/{id}",
  "GET /apps/{app}/releases/{id}/source",
  "GET /apps/{app}/releases/{id}/source/{path}",
  "PUT /apps/{app}/config",
  "GET /apps/{app}/requirements",
  "PUT /apps/{app}/resources/{key}",
  "DELETE /apps/{app}/resources/{key}",
  "PUT /apps/{app}/grants",
  "DELETE /apps/{app}/grants/{type}/{name}",
  "GET /requests",
  "POST /requests",
  "POST /requests/{id}/withdraw",
  "POST /requests/{id}/approve",
  "POST /requests/{id}/deny",
  "PUT /apps/{app}/delegation/{kind}",
  "DELETE /apps/{app}/delegation/{kind}",
  "GET /platform",
  "PUT /platform/settings/{key}",
  "PUT /platform/admins",
  "PUT /platform/egress",
  "PUT /apps/{app}/egress",
  "POST /platform/self-check",
  "GET /audit",
  "GET /metrics",
] as const;

export type ApiRoute = (typeof API_ROUTES)[number];

export type PathValue = string | number;

/** Query parameters; `undefined` values are left out of the URL. */
export type RouteQuery = Record<string, PathValue | undefined>;

/** Release numbers and request ids start at 1 (v1-data-model.md: `release_no > 0`, `request_id` identity). */
const MIN_NUMERIC_ID = 1;

function isSafeSegment(value: PathValue, text: string): boolean {
  // A number is an id: `NaN`, `Infinity`, `1.5`, `0` and `-1` are no id the
  // server could have issued, and `String()` would write them into the URL.
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value >= MIN_NUMERIC_ID;
  }
  // "/" would be read as a separator by the server; "." and ".." could climb
  // out of the route. No legitimate slug, key, email or file name is one.
  return text !== "" && text !== "." && text !== ".." && !text.includes("/");
}

function encodeSegment(value: PathValue): string {
  const text = String(value);
  if (!isSafeSegment(value, text)) {
    throw new Error(
      `Invalid path segment ${JSON.stringify(text)}; it cannot be sent to the API.`,
    );
  }
  return encodeURIComponent(text);
}

export function expandRoute(
  route: ApiRoute,
  params: Record<string, PathValue>,
): { method: string; url: string } {
  const separator = route.indexOf(" ");
  const method = route.slice(0, separator);
  const path = route
    .slice(separator + 1)
    .replace(/\{(\w+)\}/g, (_match, name: string) => {
      const value = params[name];
      if (value === undefined) {
        throw new Error(`Route ${route} needs the parameter ${name}.`);
      }
      // The source file path is the one parameter that spans segments.
      return name === "path"
        ? String(value).split("/").map(encodeSegment).join("/")
        : encodeSegment(value);
    });
  return { method, url: `${API_BASE_URL}${path}` };
}

/**
 * Strictly percent-encoded (a space is `%20`, a `+` is `%2B`), as the contract
 * says of the `reason` of a delegation revoke: `URLSearchParams` would write a
 * space as `+`.
 */
export function withQuery(url: string, query: RouteQuery | undefined): string {
  const pairs = Object.entries(query ?? {})
    .filter(([, value]) => value !== undefined)
    .map(
      ([name, value]) =>
        `${encodeURIComponent(name)}=${encodeURIComponent(String(value))}`,
    );
  return pairs.length === 0 ? url : `${url}?${pairs.join("&")}`;
}
