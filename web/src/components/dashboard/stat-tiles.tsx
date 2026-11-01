import { CapacityStrip } from "@/components/capacity-strip";
import { levelFill } from "@/lib/capacity";
import type { CapacityLevel } from "@/lib/capacity";
import type { MeterTone, StatTile } from "@/lib/dashboard/model";

const LEVEL_BY_TONE = {
  success: "normal",
  warning: "warning",
  danger: "danger",
} as const satisfies Record<MeterTone, CapacityLevel>;

/**
 * The headline numbers. Each tile is a term and its description, so a screen
 * reader reads "Memory peak, 6,000 MB, 84% of the 7,168 MB limit"; the strip
 * repeats it as cells and never carries it alone.
 */
export function StatTiles({ tiles }: { tiles: readonly StatTile[] }) {
  return (
    <dl className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,10rem),1fr))] gap-4">
      {tiles.map((tile) => (
        <div
          key={tile.key}
          className="flex min-w-0 flex-col gap-1 rounded-md border bg-card px-4 py-4"
        >
          <dt className="font-semibold text-muted-foreground">{tile.label}</dt>
          <dd className="text-xl font-bold wrap-break-word text-balance tabular-nums">
            {tile.value}
          </dd>
          {tile.meter === null ? null : (
            <dd>
              <CapacityStrip
                label={tile.meter.label}
                segments={[
                  {
                    key: "used",
                    label: tile.meter.label,
                    amount: tile.meter.value,
                    fill: levelFill(LEVEL_BY_TONE[tile.meter.tone]),
                    valueText: tile.value,
                  },
                ]}
                total={tile.meter.max}
                hasLegend={false}
              />
            </dd>
          )}
          {tile.detail === null ? null : (
            <dd className="text-muted-foreground">{tile.detail}</dd>
          )}
        </div>
      ))}
    </dl>
  );
}
