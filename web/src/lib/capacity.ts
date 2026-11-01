/**
 * How a measured quantity against its limit (memory, today) becomes a strip of
 * square cells. Pure functions, so the fill and the thresholds are tested
 * without rendering; `components/capacity-strip.tsx` only paints the result.
 *
 * The strip is counted, not measured: a cell is a fixed share of the scale, so
 * no width has to be written as an inline style (the CSP forbids one) and a
 * person can read the fill by counting.
 */

/** Cells in a strip: each is 5% of the scale. */
export const CAPACITY_CELL_COUNT = 20;

/**
 * Where a strip leaves neutral ink. These are display thresholds, not a rule of
 * the platform: admission control (design 5.7) refuses what does not fit at any
 * fill level. The percentage printed beside the strip means the colour is never
 * the only signal.
 */
export const CAPACITY_WARNING_RATIO = 0.75;
export const CAPACITY_DANGER_RATIO = 0.9;

export type CapacityLevel = "normal" | "warning" | "danger";

/**
 * What one cell is: `reserved` is set aside (appmux's own share), `used` is
 * neutral ink, `warning` and `danger` are the used share past a threshold, and
 * `free` is what is left.
 */
export type CapacityFill = "reserved" | "used" | "warning" | "danger" | "free";

export interface CapacityAmount {
  amount: number;
  fill: CapacityFill;
}

/** Normal until the used share reaches the warning ratio; normal too when there is no scale to compare against. */
export function capacityLevel(used: number, total: number): CapacityLevel {
  if (!Number.isFinite(used) || !Number.isFinite(total) || total <= 0) {
    return "normal";
  }
  const ratio = used / total;
  if (ratio >= CAPACITY_DANGER_RATIO) return "danger";
  if (ratio >= CAPACITY_WARNING_RATIO) return "warning";
  return "normal";
}

/** The fill of the used share at a level: neutral ink normally, the tone's own fill past a threshold. */
export function levelFill(level: CapacityLevel): CapacityFill {
  if (level === "warning") return "warning";
  if (level === "danger") return "danger";
  return "used";
}

function usableAmount(amount: number): number {
  return Number.isFinite(amount) && amount > 0 ? amount : 0;
}

/**
 * Cells for each amount out of `total`. The whole fill is rounded once and
 * shared out by largest remainder, so the parts add up to it; an amount above
 * zero always gets a cell, so a small use is not invisible; the cells never
 * exceed `cellCount`, which is what an over-committed slot (amounts above the
 * total) shows: a full strip.
 */
export function allocateCells(
  amounts: readonly number[],
  total: number,
  cellCount: number = CAPACITY_CELL_COUNT,
): number[] {
  const usable = amounts.map(usableAmount);
  const filledAmount = usable.reduce((sum, amount) => sum + amount, 0);
  if (!Number.isFinite(total) || total <= 0 || filledAmount === 0) {
    return usable.map(() => 0);
  }
  const scale = Math.max(total, filledAmount);
  const exact = usable.map((amount) => (amount / scale) * cellCount);
  const cells = exact.map((share) => Math.floor(share));
  usable.forEach((amount, index) => {
    if (amount > 0 && cells[index] === 0) cells[index] = 1;
  });

  const target = Math.max(
    Math.round((filledAmount / scale) * cellCount),
    cells.reduce((sum, count) => sum + count, 0),
  );
  let drawn = cells.reduce((sum, count) => sum + count, 0);
  while (drawn < target) {
    let best = -1;
    let bestRemainder = -1;
    exact.forEach((share, index) => {
      if (usable[index] === 0) return;
      const remainder = share - Math.floor(share);
      if (remainder > bestRemainder) {
        best = index;
        bestRemainder = remainder;
      }
    });
    cells[best] = (cells[best] ?? 0) + 1;
    // A part that already took its remainder goes to the back of the line.
    exact[best] = Math.floor(exact[best] ?? 0);
    drawn += 1;
  }
  while (drawn > cellCount) {
    let largest = 0;
    cells.forEach((count, index) => {
      if (count > (cells[largest] ?? 0)) largest = index;
    });
    cells[largest] = (cells[largest] ?? 1) - 1;
    drawn -= 1;
  }
  return cells;
}

/**
 * The strip, one fill per cell, in the order of the segments. Segments of fill
 * `free` are not drawn: the rest of the strip is free, whatever is left.
 */
export function capacityCells(
  segments: readonly CapacityAmount[],
  total: number,
  cellCount: number = CAPACITY_CELL_COUNT,
): CapacityFill[] {
  const drawn = segments.filter((segment) => segment.fill !== "free");
  const counts = allocateCells(
    drawn.map((segment) => segment.amount),
    total,
    cellCount,
  );
  const cells: CapacityFill[] = [];
  drawn.forEach((segment, index) => {
    for (let count = 0; count < (counts[index] ?? 0); count += 1) {
      cells.push(segment.fill);
    }
  });
  while (cells.length < cellCount) cells.push("free");
  return cells;
}
