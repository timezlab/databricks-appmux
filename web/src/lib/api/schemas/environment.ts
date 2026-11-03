/** GET /apps/{app}/environment: what an app's child is started with. */
import { z } from "./zod.ts";

export const AppEnvironment = z.object({
  /** The active release the figures describe; null when the app has none. */
  release: z.int().nullable(),
  runtime: z.object({
    kind: z.enum(["python", "node", "unknown"]),
    python_version: z.string().nullable(),
    node_version: z.string().nullable(),
  }),
  variables: z.array(
    z
      .object({
        name: z.string(),
        /** Null for a secret, and for a resource key the app does not have. */
        value: z.string().nullable(),
        /** `config`: an env entry; `resource`: a `valueFrom`, shown by its key; `platform`: set by appmux for every child. */
        source: z.enum(["config", "resource", "platform"]),
        resource_key: z.string().nullable(),
        is_secret: z.boolean(),
      })
      .refine(
        (variable) => !variable.is_secret || variable.value === null,
        "a secret's value is never returned",
      ),
  ),
  packages: z.array(
    z.object({
      name: z.string(),
      /** A specifier or range as declared, an exact version for a wheel or the platform's. */
      version: z.string().nullable(),
      /** `release`: requirements.txt or package.json; `wheel`: resolved for this release; `platform`: the platform's own environment. */
      source: z.enum(["release", "wheel", "platform"]),
    }),
  ),
});

export type AppEnvironment = z.infer<typeof AppEnvironment>;
