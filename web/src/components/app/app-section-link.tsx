import Link from "next/link";
import type { ReactNode } from "react";

import { uiButtonLook } from "@/components/button";
import type { ButtonSize, ButtonVariant } from "@/components/button";
import { Button as UiButton } from "@/components/ui/button";
import { appPagePath } from "@/lib/app/params";
import type { AppSection } from "@/lib/app/params";

export interface AppSectionLinkProps {
  slug: string;
  section: AppSection;
  children: ReactNode;
  /** Draws the link as a button of this variant. Default: an ordinary link. */
  buttonVariant?: ButtonVariant;
  size?: ButtonSize;
}

/**
 * A link to another section of the same app ("Change the memory budget" goes
 * to Settings). A slug that is not a slug gives plain text, never an `href`.
 */
export function AppSectionLink({
  slug,
  section,
  children,
  buttonVariant,
  size = "small",
}: AppSectionLinkProps) {
  const href = appPagePath(slug, section);
  if (href === null) return <span>{children}</span>;
  if (buttonVariant === undefined) return <Link href={href}>{children}</Link>;
  return (
    <UiButton asChild {...uiButtonLook(buttonVariant, size)}>
      <Link href={href}>{children}</Link>
    </UiButton>
  );
}
