/**
 * Opening an app in one of the two modes of a platform admin (design 9.1).
 *
 * "Open without token" is the default, and "Open as a user" lets the app act
 * with the admin's token. The choice is kept in an edge cookie per app
 * (`POST /apps/{app}/open-mode`), so both buttons save their mode before they
 * navigate: pressing "without token" has to switch an earlier "as a user"
 * back. If the save fails the page stays where it is, because the old cookie
 * may still say "as a user".
 */
import type { OpenMode, OpenModeRequest } from "@/lib/api";
import { appLink } from "@/lib/slug-link";

export const OPEN_MODE_LABEL: Record<OpenMode, string> = {
  without_token: "Open without token",
  as_user: "Open as a user",
};

/** The lines of the "What is the difference?" disclosure: what each mode means, and that the choice sticks. */
export const OPEN_MODE_EXPLANATION = {
  intro:
    "These apps pass each visitor's Databricks token to their code (OBO), so you can open them in two ways.",
  withoutToken:
    "The app sees your email, but anything it does as you fails. This is the default.",
  asUser:
    "The app can act as you within its scopes. This is written to the audit log.",
  remembered:
    "Your choice is remembered for each app until you pick the other one.",
} as const;

export function openAsUserPrompt(appName: string): string {
  return `Open ${appName} as a user? It will receive your Databricks token and can act as you; this is recorded in the audit log.`;
}

export interface OpenDependencies {
  setMode: (slug: string, body: OpenModeRequest) => Promise<void>;
  /** Goes to `href` in this tab (`window.location.assign`). */
  navigate: (href: string) => void;
}

export type OpenResult =
  | { status: "opened" }
  | { status: "invalid-slug" }
  | { status: "failed"; error: unknown };

export async function openAppWithMode(
  slug: string,
  mode: OpenMode,
  dependencies: OpenDependencies,
): Promise<OpenResult> {
  const href = appLink(slug);
  if (href === null) return { status: "invalid-slug" };
  try {
    await dependencies.setMode(slug, { mode });
  } catch (error) {
    // Handled by the caller, which shows it; nothing is navigated meanwhile.
    return { status: "failed", error };
  }
  dependencies.navigate(href);
  return { status: "opened" };
}

/** "as a user" hands the admin's token to the app and writes an audit row, so it takes a second press. */
export function openModeNeedsConfirmation(mode: OpenMode): boolean {
  return mode === "as_user";
}
