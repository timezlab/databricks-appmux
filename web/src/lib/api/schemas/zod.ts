import { z } from "zod";

// Zod builds a code-generated fast path for object schemas and first probes
// `new Function("")` to learn whether eval is allowed. Under the strict CSP of
// the admin page (design 10.1: `script-src 'self'` plus script hashes, no
// 'unsafe-eval') that probe is itself a violation report even though the throw
// is caught. Zod's own comment, node_modules/zod/v4/core/util.js
// (`allowsEval`): "strict CSPs report the caught `new Function` as a
// `securitypolicyviolation` even though the throw is swallowed". `jitless` skips
// the probe; it must run before the first object schema is built, so every
// schema file takes `z` from this module, the only one that imports `zod`
// (eslint.config.mjs enforces that). tests/contract/csp.test.ts guards it.
z.config({ jitless: true });

export { z };
