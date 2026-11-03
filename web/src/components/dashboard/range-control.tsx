"use client";

import { useId } from "react";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { TIME_RANGES, isTimeRangeId } from "@/lib/dashboard/scope";
import type { TimeRangeId } from "@/lib/dashboard/scope";

export interface RangeControlProps {
  rangeId: TimeRangeId;
  onRangeChange: (id: TimeRangeId) => void;
}

/**
 * The time range as a row of toggles, one pressed: six short labels fit a
 * phone's width, so every range is one press away at every size. Each toggle's
 * name starts with its visible text ("7d") and spells the range out after it.
 */
export function RangeControl({ rangeId, onRangeChange }: RangeControlProps) {
  const labelId = useId();
  const hintId = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <span id={labelId} className="text-sm leading-5 font-medium">
        Time range
      </span>
      <ToggleGroup
        type="single"
        variant="outline"
        size="sm"
        value={rangeId}
        onValueChange={(value) => {
          // "" when the pressed toggle is pressed again: a range is always chosen, so it stays.
          if (isTimeRangeId(value)) onRangeChange(value);
        }}
        aria-labelledby={labelId}
        aria-describedby={hintId}
      >
        {TIME_RANGES.map((range) => (
          <ToggleGroupItem
            key={range.id}
            value={range.id}
            aria-label={`${range.shortLabel}, ${range.label.toLowerCase()}`}
            className="px-2.5 tabular-nums"
          >
            {range.shortLabel}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <p id={hintId} className="text-xs text-muted-foreground">
        Ends at the last full minute, in UTC.
      </p>
    </div>
  );
}
