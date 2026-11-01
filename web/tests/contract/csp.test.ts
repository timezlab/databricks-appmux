import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import { describe, it } from "vitest";

const execFileAsync = promisify(execFile);

const UI_DIR = path.resolve(import.meta.dirname, "../..");
const SCHEMAS_URL = pathToFileURL(
  path.join(UI_DIR, "src/lib/api/schemas/index.ts"),
).href;

// Each test starts a Node process that loads Zod; vitest's default 5 s is too
// tight when the suite runs in parallel on a busy machine.
const CHILD_PROCESS_TIMEOUT_MS = 20_000;

// The probe runs in a child process so that the module graph (and Zod's cached
// `allowsEval` result) is fresh. It replaces the global `Function` with a
// recorder BEFORE importing anything, then prints every dynamic-code
// construction it saw as JSON. Under the design's strict CSP
// (`script-src 'self' <hashes>` with no 'unsafe-eval') each such construction is
// a `securitypolicyviolation` report, even when the throw is caught.
const PROBE = `
const constructed = [];
const RealFunction = globalThis.Function;
globalThis.Function = new Proxy(RealFunction, {
  construct(target, args, newTarget) {
    constructed.push(args.map(String));
    return Reflect.construct(target, args, newTarget);
  },
  apply(target, thisArg, args) {
    constructed.push(args.map(String));
    return Reflect.apply(target, thisArg, args);
  },
});

if (process.env.PROBE_MODE === "control") {
  // Zod on its own, with its default configuration.
  const { z } = await import("zod");
  z.object({ a: z.string() }).parse({ a: "x" });
} else {
  const schemas = await import(process.env.SCHEMAS_URL);
  schemas.MeResponse.parse({
    email: "alice@example.com",
    groups: [],
    is_platform_admin: false,
  });
}
console.log(JSON.stringify(constructed));
`;

async function dynamicCodeConstructions(
  mode: "control" | "schemas",
): Promise<string[][]> {
  const { stdout } = await execFileAsync(
    process.execPath,
    ["--input-type=module", "-e", PROBE],
    {
      cwd: UI_DIR,
      env: { ...process.env, PROBE_MODE: mode, SCHEMAS_URL },
    },
  );
  return JSON.parse(stdout) as string[][];
}

describe("strict CSP (no 'unsafe-eval')", () => {
  it(
    "the probe sees the Function probe of Zod's default configuration",
    { timeout: CHILD_PROCESS_TIMEOUT_MS },
    async () => {
      // Control: without it, a Zod release that stops probing would make the next
      // test pass for the wrong reason, and a broken recorder would pass it too.
      const constructed = await dynamicCodeConstructions("control");
      assert.ok(
        constructed.length > 0,
        "Zod no longer calls new Function; re-check the jitless setting",
      );
    },
  );

  it(
    "importing the schemas and parsing a body constructs no Function",
    { timeout: CHILD_PROCESS_TIMEOUT_MS },
    async () => {
      // node_modules/zod/v4/core/util.js (`allowsEval`): "strict CSPs report the
      // caught `new Function` as a `securitypolicyviolation` even though the
      // throw is swallowed". schemas/zod.ts sets `jitless` to skip that probe.
      assert.deepEqual(await dynamicCodeConstructions("schemas"), []);
    },
  );
});
