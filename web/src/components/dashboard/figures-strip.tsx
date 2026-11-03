import { cn } from "cn";
import {
  ArrowDownRightIcon,
  ArrowRightIcon,
  ArrowUpRightIcon,
} from "lucide-react";

import { SkeletonFigure } from "@/components/skeletons";
import { sparklinePath } from "@/lib/dashboard/figures";
import type { Figure, FigureChange } from "@/lib/dashboard/figures";

/** The sparkline's drawing box; the SVG stretches it to the figure's width. */
const SPARK_WIDTH = 120;
const SPARK_HEIGHT = 28;
/** As many placeholders as `buildFigures` gives an app, and the platform (which adds the apps running). */
const APP_FIGURES = 6;
const PLATFORM_FIGURES = 7;

/** The cells sit in a grid that overhangs its frame by a pixel on each side, so only the hairlines between cells show. */
const STRIP_FRAME = "overflow-hidden rounded-lg border bg-card";
const STRIP_GRID =
  "-m-px grid grid-cols-[repeat(auto-fill,minmax(min(100%,11rem),1fr))]";
const STRIP_CELL = "flex min-w-0 flex-col gap-1 border-t border-l p-4";

function Sparkline({ values }: { values: readonly (number | null)[] }) {
  const path = sparklinePath(values, SPARK_WIDTH, SPARK_HEIGHT);
  return (
    <svg
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
      className="mt-1 h-7 w-full overflow-visible"
    >
      {path === "" ? null : (
        <path
          d={path}
          fill="none"
          className="stroke-chart-1"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  );
}

const CHANGE_ICONS = {
  up: ArrowUpRightIcon,
  down: ArrowDownRightIcon,
  same: ArrowRightIcon,
} as const;

/** The change in words; the arrow repeats the direction and is never its only carrier, nor coloured as good or bad. */
function Change({ change }: { change: FigureChange }) {
  const Icon = CHANGE_ICONS[change.direction];
  return (
    <p className="flex items-start gap-1 text-xs text-muted-foreground">
      <Icon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span>{change.text}</span>
    </p>
  );
}

/**
 * The figures over the window as one strip, divided by hairlines: each a
 * number in tabular digits with its unit, what it means, a sparkline of the
 * window and, where there was a window before, how it compares.
 */
export function FiguresStrip({ figures }: { figures: readonly Figure[] }) {
  return (
    <div className={STRIP_FRAME}>
      <dl className={STRIP_GRID}>
        {figures.map((figure) => (
          <div key={figure.key} className={STRIP_CELL}>
            <dt className="text-sm text-muted-foreground">{figure.label}</dt>
            <dd className="flex flex-wrap items-baseline gap-x-1">
              <span className="text-2xl leading-8 font-semibold tabular-nums">
                {figure.value}
              </span>
              {figure.unit === null ? null : (
                <span className="text-sm text-muted-foreground">
                  {figure.unit}
                </span>
              )}
            </dd>
            {figure.detail === null ? null : (
              <dd className="text-xs text-muted-foreground">{figure.detail}</dd>
            )}
            <dd>
              <Sparkline values={figure.spark} />
              <span className="sr-only">{figure.sparkText}</span>
            </dd>
            {figure.change === null ? null : (
              <dd>
                <Change change={figure.change} />
              </dd>
            )}
          </div>
        ))}
      </dl>
    </div>
  );
}

/** The strip's shape while the figures load; hidden from screen readers (the panel says what is loading). */
export function FiguresStripSkeleton({ isPlatform }: { isPlatform: boolean }) {
  const count = isPlatform ? PLATFORM_FIGURES : APP_FIGURES;
  return (
    <div className={STRIP_FRAME} aria-hidden="true" data-skeleton="figures">
      <div className={STRIP_GRID}>
        {Array.from({ length: count }, (_, index) => (
          // `*:min-w-0` lets the placeholder shrink with a narrow cell, as the figure it stands for does.
          <div key={index} className={cn(STRIP_CELL, "*:min-w-0")}>
            <SkeletonFigure />
          </div>
        ))}
      </div>
    </div>
  );
}
