import type { ReactNode } from "react";

import type { Tone } from "@/lib/status-presentation";
import { Alert, AlertDescription, AlertTitle } from "./ui/alert";

/**
 * How a screen reader hears the notice when it appears:
 * - `assertive`: at once, interrupting (`role="alert"`), for an error;
 * - `polite`: after the current speech (`role="status"`), for a result like "Saved";
 * - `off`: not at all, for a notice that is part of the page when it loads.
 */
export type NoticeLive = "assertive" | "polite" | "off";

export interface NoticeProps {
  tone: Tone;
  /** One short line; it is bold, and the tone is also carried by the left bar and the colours. */
  title: string;
  /** Default "off". */
  live?: NoticeLive;
  /** More words, or a button; shown under the title. */
  children?: ReactNode;
}

const ROLE: Record<NoticeLive, "alert" | "status" | undefined> = {
  assertive: "alert",
  polite: "status",
  off: undefined,
};

/** A framed message. The title is text, never markup: React escapes what it is given. */
export function Notice({ tone, title, live = "off", children }: NoticeProps) {
  return (
    // `Alert` is `role="alert"` by default; `live` decides here, and "off" means none.
    <Alert variant={tone} role={ROLE[live]}>
      <AlertTitle>{title}</AlertTitle>
      {children === undefined ? null : (
        <AlertDescription>{children}</AlertDescription>
      )}
    </Alert>
  );
}
