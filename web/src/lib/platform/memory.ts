/**
 * The slot's memory as the platform page shows it (design 5.7, 10.1): what the
 * container allows, what appmux keeps for itself, what the running apps have
 * committed, and what is left. The four figures come from `GET /platform`
 * (`memory`); this only chooses the words and the colour.
 */
import type { PlatformApp, PlatformView } from "@/lib/api/schemas";
import { capacityLevel, levelFill } from "@/lib/capacity";
import type { CapacityFill, CapacityLevel } from "@/lib/capacity";
import { UNKNOWN_TEXT, formatCount, formatPercent } from "@/lib/format";

/** One part of the memory strip: reserved for appmux, committed to apps, or free. */
export interface MemorySegment {
  key: "reserved" | "committed" | "free";
  label: string;
  /** In MB; the strip draws `free` as whatever is left, but the legend prints it. */
  amount: number;
  fill: CapacityFill;
  valueText: string;
}

export interface MemoryView {
  limitMb: number;
  reserveMb: number;
  /** `limit - reserve`: what the apps share. */
  availableMb: number;
  committedMb: number;
  /** As the server computed it; negative when the slot is over-committed. */
  freeMb: number;
  percentText: string;
  /** Neutral until the committed share of `availableMb` passes a display threshold. */
  level: CapacityLevel;
  /** Words for a level past normal ("Getting full"), shown with the percentage; `null` when normal. */
  levelText: string | null;
  headline: string;
  /** Reserved, committed, free: the strip and its legend. The strip's scale is `limitMb`. */
  segments: MemorySegment[];
  /** The whole reading in one sentence, the strip's accessible name. */
  summary: string;
  /** A sentence for the cases the figures alone do not explain, or `null`. */
  note: string | null;
}

function levelText(
  level: CapacityLevel,
  availableMb: number,
  freeMb: number,
): string | null {
  if (level === "normal") return null;
  if (availableMb <= 0) return "No memory available";
  if (level === "warning") return "Getting full";
  return freeMb < 0 ? "Over-committed" : "Almost full";
}

export function memoryView(memory: PlatformView["memory"]): MemoryView {
  const availableMb = memory.limit_mb - memory.reserve_mb;
  const committedMb = memory.committed_mb;
  const freeMb = memory.free_mb;
  const percentText =
    availableMb > 0 ? formatPercent(committedMb, availableMb) : UNKNOWN_TEXT;

  let note: string | null = null;
  if (availableMb <= 0) {
    note =
      "The reserve is as large as the container limit, so no memory is available to apps.";
  } else if (freeMb < 0) {
    note = `The slot is over-committed by ${formatCount(-freeMb)} MB.`;
  }

  const headline =
    availableMb > 0
      ? `${formatCount(committedMb)} of ${formatCount(availableMb)} MB committed (${percentText}); ${formatCount(Math.max(freeMb, 0))} MB free`
      : `${formatCount(committedMb)} MB committed; no memory available to apps`;

  const level =
    availableMb <= 0 ? "danger" : capacityLevel(committedMb, availableMb);
  const freeShownMb = Math.max(freeMb, 0);
  const segments: MemorySegment[] = [
    {
      key: "reserved",
      label: "Reserved for appmux",
      amount: memory.reserve_mb,
      fill: "reserved",
      valueText: `${formatCount(memory.reserve_mb)} MB`,
    },
    {
      key: "committed",
      label: "Committed to apps",
      amount: committedMb,
      fill: levelFill(level),
      valueText: `${formatCount(committedMb)} MB`,
    },
    {
      key: "free",
      label: "Free",
      amount: freeShownMb,
      fill: "free",
      valueText: `${formatCount(freeShownMb)} MB`,
    },
  ];
  const summary =
    `Memory: ${formatCount(memory.reserve_mb)} MB reserved for appmux, ` +
    `${formatCount(committedMb)} MB committed to apps, ` +
    `${formatCount(freeShownMb)} MB free, of the ${formatCount(memory.limit_mb)} MB the container allows.`;

  return {
    limitMb: memory.limit_mb,
    reserveMb: memory.reserve_mb,
    availableMb,
    committedMb,
    freeMb,
    percentText,
    level,
    levelText: levelText(level, availableMb, freeMb),
    headline,
    segments,
    summary,
    note,
  };
}

/** The budgets of all registered apps, running or not (the sum a full start would need before per-child overhead). */
export function totalBudgetMb(
  apps: readonly Pick<PlatformApp, "memory_budget_mb">[],
): number {
  return apps.reduce((total, app) => total + app.memory_budget_mb, 0);
}
