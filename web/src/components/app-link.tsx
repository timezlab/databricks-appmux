import type { ReactNode } from "react";

import { appLink } from "@/lib/slug-link";
import { uiButtonLook } from "./button";
import type { ButtonSize, ButtonVariant } from "./button";
import { Button as UiButton } from "./ui/button";

export interface AppLinkProps {
  /** The app's slug from the API. Checked against the contract pattern before it becomes a path. */
  slug: string;
  children: ReactNode;
  /** Draws the link as a button of this variant. Default: an ordinary link. */
  buttonVariant?: ButtonVariant;
  size?: ButtonSize;
}

/**
 * The link to a child app: `/<slug>/` on this same origin. It is a plain `<a>`,
 * not `next/link`, because the child's route lives outside the admin UI's base
 * path (`/_appmux`): `next/link` would prefix it and navigate inside the UI
 * instead of loading the app. A slug that fails the pattern (path traversal,
 * the reserved `_appmux`, a malformed value from a bad response) is shown as
 * plain text with no link, never as an `href`.
 */
export function AppLink({
  slug,
  children,
  buttonVariant,
  size = "normal",
}: AppLinkProps) {
  const href = appLink(slug);
  if (href === null) return <span>{children}</span>;
  if (buttonVariant === undefined) return <a href={href}>{children}</a>;
  return (
    <UiButton asChild {...uiButtonLook(buttonVariant, size)}>
      <a href={href}>{children}</a>
    </UiButton>
  );
}
