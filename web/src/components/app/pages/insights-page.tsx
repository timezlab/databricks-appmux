"use client";

import { useState } from "react";

import { MetricsPanel } from "@/components/dashboard/metrics-panel";
import { RangeControl } from "@/components/dashboard/range-control";
import { Section } from "@/components/section";
import {
  DEFAULT_RANGE_ID,
  TIME_RANGES,
  timeRangeById,
} from "@/lib/dashboard/scope";
import type { TimeRangeId } from "@/lib/dashboard/scope";
import { useAppPageContext } from "../app-context";

/** Used only if the range state holds an id the list does not (it cannot: the control offers the list). */
const FALLBACK_RANGE = TIME_RANGES[1];

/**
 * `/app/insights/`: the dashboard's charts for this app alone (requests with
 * their errors, response time, memory, egress drops), over a range chosen here.
 * The platform's breakdown by app stays on the dashboard.
 */
export function AppInsightsPage() {
  const { app } = useAppPageContext();
  const [rangeId, setRangeId] = useState<TimeRangeId>(DEFAULT_RANGE_ID);
  const range = timeRangeById(rangeId) ?? FALLBACK_RANGE;
  return (
    <Section title="Insights">
      <RangeControl rangeId={rangeId} onRangeChange={setRangeId} />
      <MetricsPanel
        scope={{ kind: "app", slug: app.slug }}
        range={range}
        onRangeChange={setRangeId}
      />
    </Section>
  );
}
