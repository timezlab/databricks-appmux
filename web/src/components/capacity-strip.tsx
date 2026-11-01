import { capacityCells } from "@/lib/capacity";
import type { CapacityFill } from "@/lib/capacity";
import { toneMark } from "@/lib/status-presentation";
import type { Tone } from "@/lib/status-presentation";

export interface CapacityStripSegment {
  key: string;
  /** Legend name ("Committed to apps"). */
  label: string;
  /** In the unit of `total`. */
  amount: number;
  fill: CapacityFill;
  /** Legend figure, formatted by the caller ("5,775 MB"). */
  valueText: string;
}

export interface CapacityStripProps {
  /** The whole reading in one sentence: the strip's accessible name. */
  label: string;
  /** In order from the start of the strip; the rest of the strip is free. */
  segments: readonly CapacityStripSegment[];
  /** The scale: the amount the full strip stands for. */
  total: number;
  /** The figure printed beside the strip ("81% committed"). */
  figure?: string;
  /** Words for a level past normal ("Getting full"), drawn with a mark in the level's tone. */
  levelText?: string | null;
  level?: "normal" | "warning" | "danger";
  /** Names every fill under the strip. Off for a small strip whose figures are already printed next to it. */
  hasLegend?: boolean;
}

// Flat square cells in the closed palette. The three fills differ by shape
// before any hue or lightness: solid ink (used), an ink outline (reserved, set
// aside and not in use) and a faint flat track (free). Lightness alone could
// not do it: in dark mode the grey between ink and track is under 3:1 from
// both. Amber and red appear only past the display thresholds, and the figure
// and `levelText` say the same in words.
const CELL_CLASSES = {
  reserved: "border-2 border-foreground bg-background",
  used: "bg-foreground",
  warning: "bg-tone-warning-border",
  danger: "bg-tone-danger-border",
  free: "bg-accent",
} as const satisfies Record<CapacityFill, string>;

const LEVEL_TONE = {
  warning: "warning",
  danger: "danger",
} as const satisfies Record<"warning" | "danger", Tone>;

const LEVEL_TEXT_CLASSES = {
  warning: "text-tone-warning-text",
  danger: "text-tone-danger-text",
} as const satisfies Record<"warning" | "danger", string>;

/**
 * A capacity as a strip of square cells (`lib/capacity.ts` counts them), with
 * the figure beside it and a legend under it. No `style` attribute: the fill
 * is the number of cells, not a width. The strip is one image for a screen
 * reader (`label`); the legend and the figure are plain text.
 */
export function CapacityStrip({
  label,
  segments,
  total,
  figure,
  levelText = null,
  level = "normal",
  hasLegend = true,
}: CapacityStripProps) {
  const cells = capacityCells(segments, total);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <div
          role="img"
          aria-label={label}
          className="flex h-4 min-w-32 flex-1 gap-0.5"
        >
          {cells.map((fill, index) => (
            <span
              // Cells are positional and never reorder, so the index is the identity.
              key={index}
              aria-hidden="true"
              className={`h-full min-w-0 flex-1 rounded-none ${CELL_CLASSES[fill]}`}
            />
          ))}
        </div>
        {figure === undefined && levelText === null ? null : (
          <p className="flex shrink-0 flex-wrap items-baseline gap-x-2 font-semibold tabular-nums">
            {figure === undefined ? null : <span>{figure}</span>}
            {levelText === null || level === "normal" ? null : (
              <span className={LEVEL_TEXT_CLASSES[level]}>
                <span aria-hidden="true">{toneMark(LEVEL_TONE[level])} </span>
                {levelText}
              </span>
            )}
          </p>
        )}
      </div>
      {hasLegend ? (
        <ul className="flex flex-wrap gap-x-4 gap-y-1">
          {segments.map((segment) => (
            <li key={segment.key} className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className={`size-3 shrink-0 rounded-none ${CELL_CLASSES[segment.fill]}`}
              />
              <span className="text-muted-foreground">{segment.label}</span>
              <span className="font-semibold tabular-nums">
                {segment.valueText}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
