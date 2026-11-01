"use client";

import { TIME_RANGES, isTimeRangeId } from "@/lib/dashboard/scope";
import type { ScopeOption, TimeRangeId } from "@/lib/dashboard/scope";
import { Field } from "@/components/field";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";

export interface DashboardControlsProps {
  options: readonly ScopeOption[];
  scopeKey: string;
  onScopeChange: (key: string) => void;
  rangeId: TimeRangeId;
  onRangeChange: (id: TimeRangeId) => void;
}

/** What to show and for how long: two native selects, so keyboard and screen readers work as they do everywhere. */
export function DashboardControls({
  options,
  scopeKey,
  onScopeChange,
  rangeId,
  onRangeChange,
}: DashboardControlsProps) {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,16rem),1fr))] items-start gap-4">
      <Field
        label="Show"
        hint="The platform, every app side by side, or one app."
      >
        {(control) => (
          <NativeSelect
            {...control}
            value={scopeKey}
            onChange={(event) => {
              onScopeChange(event.target.value);
            }}
          >
            {options.map((option) => (
              <NativeSelectOption key={option.key} value={option.key}>
                {option.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        )}
      </Field>
      <Field
        label="Time range"
        hint="Ends at the last full minute. One request covers at most 24 hours."
      >
        {(control) => (
          <NativeSelect
            {...control}
            value={rangeId}
            onChange={(event) => {
              if (isTimeRangeId(event.target.value)) {
                onRangeChange(event.target.value);
              }
            }}
          >
            {TIME_RANGES.map((range) => (
              <NativeSelectOption key={range.id} value={range.id}>
                {range.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        )}
      </Field>
    </div>
  );
}
