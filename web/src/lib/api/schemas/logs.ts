/** GET /apps/{app}/logs: the last lines of an app's output. */
import { z } from "./zod.ts";
import { AppState } from "./enums.ts";
import { Time } from "./primitives.ts";

/** `appmux` is a line appmux wrote about the app (started, exited with code 1 after 2.3 s). */
const OutputStream = z.enum(["stdout", "stderr", "appmux"]);

export const AppLogs = z.object({
  /** The app's state, or `never_started` when this process has started no copy of it. */
  state: z.union([AppState, z.literal("never_started")]),
  /** Changes at every start; clear the view when it changes. */
  instance: z.string().nullable(),
  lines: z.array(
    z.object({
      seq: z.int(),
      at: Time,
      stream: OutputStream,
      text: z.string(),
    }),
  ),
  /** The `seq` to send as `after` next time. */
  next_after: z.int(),
  /** Lines after `after` that the ring no longer holds or that `limit` left out. */
  dropped: z.int(),
  last_exit: z
    .object({
      code: z.int().nullable(),
      signal: z.string().nullable(),
      at: Time,
      ran_for_s: z.number(),
    })
    .nullable(),
});

export type AppLogs = z.infer<typeof AppLogs>;
