import { cn } from "cn";

import type { Tone } from "@/lib/status-presentation";

/**
 * The four states of a line, plus the two tones a status of another kind can
 * have (a pending request waits on someone: `warning`; a pending release is
 * just queued: `neutral`).
 */
export type LampState =
  "running" | "starting" | "stopped" | "failed" | "warning" | "neutral";

const DOT_CLASSES: Record<LampState, string> = {
  running: "bg-tone-success-border",
  starting: "animate-lamp-breathe bg-tone-warning-border",
  // A ring, not a dot: a stopped line has no light in it.
  stopped: "border-2 border-tone-neutral-border",
  failed: "bg-tone-danger-border",
  warning: "bg-tone-warning-border",
  neutral: "bg-tone-neutral-border",
};

const LAMP_OF_TONE: Record<Tone, LampState> = {
  success: "running",
  info: "starting",
  neutral: "stopped",
  warning: "warning",
  danger: "failed",
};

/** The lamp that draws a status of this tone (`status-presentation.ts`). */
export function lampStateOfTone(tone: Tone): LampState {
  return LAMP_OF_TONE[tone];
}

export interface LampProps {
  state: LampState;
  /** The word beside the lamp ("Running"). Always shown: the colour only backs it up. */
  label: string;
  /** One sentence about the state, shown as the hover title. */
  description?: string;
}

/**
 * A state as a small round lamp and a word, with no box around it
 * (docs/product/admin-ui-design.md, "The lamp"). The word carries the meaning;
 * the dot's colour and shape (a ring for stopped) back it up. A starting lamp
 * breathes, slower when the person asked for less motion.
 */
export function Lamp({ state, label, description }: LampProps) {
  return (
    <span
      className="inline-flex items-center gap-1.5 text-sm font-medium whitespace-nowrap text-foreground"
      data-lamp={state}
      title={description}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-2.5 shrink-0 rounded-full forced-colors:bg-[CanvasText]",
          DOT_CLASSES[state],
        )}
      />
      {label}
    </span>
  );
}
