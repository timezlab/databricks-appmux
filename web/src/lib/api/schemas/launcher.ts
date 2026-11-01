/** GET /me, GET /apps, POST /apps/{app}/open-mode. */
import { z } from "./zod.ts";
import { AppPermission, AppState, OpenMode } from "./enums.ts";

// ----------------------------------------------------------- request bodies

export const OpenModeRequest = z.strictObject({ mode: OpenMode });

export type OpenModeRequest = z.infer<typeof OpenModeRequest>;

export const MeResponse = z.object({
  email: z.string(),
  groups: z.array(z.string()),
  is_platform_admin: z.boolean(),
});

export type MeResponse = z.infer<typeof MeResponse>;

export const AppSummary = z.object({
  slug: z.string(),
  display_name: z.string(),
  state: AppState,
  permission: AppPermission,
  open_modes: z.array(OpenMode),
});

export type AppSummary = z.infer<typeof AppSummary>;

export const AppList = z.object({ items: z.array(AppSummary) });

export type AppList = z.infer<typeof AppList>;
