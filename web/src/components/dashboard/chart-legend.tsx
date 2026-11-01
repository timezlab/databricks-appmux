import type { ChartSpec } from "@/lib/dashboard/model";
import { seriesStyle } from "@/lib/dashboard/series-style";
import type { LegendItem } from "@/lib/dashboard/model";
import { Marker, TexturePattern, barFill, textureId } from "./series-marks";

const SWATCH_WIDTH = 28;
const SWATCH_HEIGHT = 14;
const LINE_STROKE_WIDTH = 2;

/**
 * A key to one series, drawn the way the series is: a bar swatch with its
 * texture, or a short stretch of line with its dash pattern and marker.
 */
function Swatch({
  kind,
  item,
  idPrefix,
}: {
  kind: ChartSpec["kind"];
  item: LegendItem;
  idPrefix: string;
}) {
  const style = seriesStyle(item.tone);
  const patternId = textureId(idPrefix, item.tone);
  return (
    <svg
      width={SWATCH_WIDTH}
      height={SWATCH_HEIGHT}
      viewBox={`0 0 ${SWATCH_WIDTH} ${SWATCH_HEIGHT}`}
      aria-hidden="true"
      focusable="false"
      className="shrink-0"
    >
      {kind === "columns" ? (
        <>
          <defs>
            <TexturePattern id={patternId} tone={item.tone} />
          </defs>
          <rect
            width={SWATCH_WIDTH}
            height={SWATCH_HEIGHT}
            rx={2}
            fill={barFill(patternId, item.tone)}
          />
        </>
      ) : (
        <>
          <line
            x1={0}
            x2={SWATCH_WIDTH}
            y1={SWATCH_HEIGHT / 2}
            y2={SWATCH_HEIGHT / 2}
            stroke={style.color}
            strokeWidth={LINE_STROKE_WIDTH}
            strokeDasharray={style.dash}
          />
          <Marker
            shape={style.marker}
            x={SWATCH_WIDTH / 2}
            y={SWATCH_HEIGHT / 2}
            color={style.color}
          />
        </>
      )}
    </svg>
  );
}

/**
 * The names of the series of a chart with two or more. One series is named by
 * the chart's title; the legend repeats each series' texture, dash and marker,
 * so no series is told apart by its colour alone.
 */
export function ChartLegend({ spec }: { spec: ChartSpec }) {
  if (spec.legend.length < 2) return null;
  const idPrefix = `texture-${spec.id}-legend`;
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
      {spec.legend.map((item) => (
        <li key={item.label} className="flex items-center gap-2">
          <Swatch kind={spec.kind} item={item} idPrefix={idPrefix} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}
