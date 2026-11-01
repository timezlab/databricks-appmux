/** The contract's examples manifest: which example file belongs to which schema and which routes. */
import path from "node:path";
import { z } from "zod";

import { EXAMPLES_DIR, readJson } from "./contract-examples";

const Manifest = z.record(
  z.string(),
  z.object({
    file: z.string(),
    direction: z.enum(["request", "response"]),
    routes: z.array(z.string()),
  }),
);

export const manifest = Manifest.parse(
  readJson(path.join(EXAMPLES_DIR, "manifest.json")),
);
