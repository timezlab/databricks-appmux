/**
 * The Environment section (`GET /apps/{app}/environment`): what the app's child
 * is started with, as words, filters and copyable text. A secret's value never
 * reaches the browser (the contract returns `null` and the schema refuses
 * anything else); these functions mask it once more, so no view, search or
 * copy can show one even if a bad answer slipped through.
 */
import type { AppEnvironment } from "@/lib/api";

type Variable = AppEnvironment["variables"][number];
type Package = AppEnvironment["packages"][number];
export type PackageSource = Package["source"];
export type PackageSourceFilter = "all" | PackageSource;

export const PACKAGE_SOURCE_FILTERS: readonly PackageSourceFilter[] = [
  "all",
  "release",
  "wheel",
  "platform",
];

/** What a secret's value reads as, everywhere. */
export const SECRET_MASK = "***";

const NOT_AVAILABLE_TEXT = "not available";
const NOT_DETECTED_TEXT = "Not detected";

/** The value as shown: the mask for a secret, "not available" for a resource key the app lacks. */
export function variableValueText(variable: Variable): string {
  if (variable.is_secret) return SECRET_MASK;
  return variable.value ?? NOT_AVAILABLE_TEXT;
}

/** "config", "platform", or "resource key db-password". */
export function variableSourceText(variable: Variable): string {
  if (variable.source === "resource") {
    return variable.resource_key === null
      ? "resource key"
      : `resource key ${variable.resource_key}`;
  }
  return variable.source;
}

function matches(query: string, ...texts: (string | null)[]): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return texts.some((text) => text?.toLowerCase().includes(needle) === true);
}

/** The variables whose name, shown value or resource key contains `query`. */
export function filterVariables(
  variables: readonly Variable[],
  query: string,
): Variable[] {
  return variables.filter((variable) =>
    matches(
      query,
      variable.name,
      variable.is_secret ? null : variable.value,
      variable.resource_key,
    ),
  );
}

/** The packages of `source` whose name or version contains `query`. */
export function filterPackages(
  packages: readonly Package[],
  query: string,
  source: PackageSourceFilter,
): Package[] {
  return packages.filter(
    (item) =>
      (source === "all" || item.source === source) &&
      matches(query, item.name, item.version),
  );
}

export interface RuntimeRow {
  label: string;
  value: string;
}

/** The runtime block: each version, or "Not detected". */
export function runtimeRows(runtime: AppEnvironment["runtime"]): RuntimeRow[] {
  return [
    { label: "Python", value: runtime.python_version ?? NOT_DETECTED_TEXT },
    { label: "Node", value: runtime.node_version ?? NOT_DETECTED_TEXT },
  ];
}

/** The JSON view: the answer as the server sent it, with every secret's value forced to `null`. */
export function environmentJson(environment: AppEnvironment): string {
  const safe: AppEnvironment = {
    ...environment,
    variables: environment.variables.map((variable) =>
      variable.is_secret ? { ...variable, value: null } : variable,
    ),
  };
  return JSON.stringify(safe, null, 2);
}

/** `NAME=value` per line, like an env file; a secret is the mask. */
export function variablesCopyText(variables: readonly Variable[]): string {
  return variables
    .map((variable) => `${variable.name}=${variableValueText(variable)}`)
    .join("\n");
}

/** `fastapi ==0.115.0 (release)` per line. */
export function packagesCopyText(packages: readonly Package[]): string {
  return packages
    .map((item) =>
      item.version === null
        ? `${item.name} (${item.source})`
        : `${item.name} ${item.version} (${item.source})`,
    )
    .join("\n");
}
