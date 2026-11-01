import {
  CartesianGrid,
  LabelList,
  Line,
  LineChart as RechartsLineChart,
  XAxis,
  YAxis,
} from "recharts";
import type { ReactNode } from "react";
import type { DotItemDotProps } from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import {
  formatAxisNumber,
  markerIndices,
  xTickIndices,
} from "@/lib/dashboard/chart";
import {
  LINE_LIMIT_KEY,
  LINE_VALUE_KEY,
  lineAxis,
  lineRows,
} from "@/lib/dashboard/chart-data";
import type { LineChartSpec } from "@/lib/dashboard/model";
import { seriesStyle } from "@/lib/dashboard/series-style";
import type { SeriesStyle } from "@/lib/dashboard/series-style";
import {
  AXIS_LINE,
  AXIS_TICK,
  CHART_SIZE_CLASS,
  CURSOR_LINE,
  GRID_STROKE,
  LABEL_FILL,
  PLOT_MARGIN,
  Y_AXIS_WIDTH,
} from "./chart-theme";
import { Marker } from "./series-marks";

const LINE_STROKE_WIDTH = 2;
const LIMIT_LABEL_OFFSET = 8;

/** The marker of a point, or nothing for a point Recharts could not place. */
function markerAt(dot: DotItemDotProps, style: SeriesStyle): ReactNode {
  if (dot.cx === undefined || dot.cy === undefined) return null;
  return (
    <Marker shape={style.marker} x={dot.cx} y={dot.cy} color={style.color} />
  );
}

/**
 * A line over time, with an optional level (the memory limit) as a dashed line
 * of its own. A minute with no data breaks the line; it is never a zero. Only
 * the newest point (and a point with no neighbour) gets a marker, so a long line
 * is not a string of beads; the limit has one at its right end.
 */
export function LineChart({ spec }: { spec: LineChartSpec }) {
  const rows = lineRows(spec);
  const scale = lineAxis(spec);
  const lineStyle = seriesStyle(spec.tone);
  const limitStyle = seriesStyle(spec.referenceTone);
  const valueMarkers = new Set(markerIndices(spec.values));
  const lastIndex = rows.length - 1;
  const hasLimit = spec.reference !== null && spec.reference > 0;
  return (
    <ChartContainer
      label={spec.title}
      description={spec.description}
      className={CHART_SIZE_CLASS}
    >
      <RechartsLineChart
        data={rows}
        margin={PLOT_MARGIN}
        accessibilityLayer={false}
      >
        <CartesianGrid vertical={false} stroke={GRID_STROKE} />
        <XAxis
          dataKey="index"
          ticks={xTickIndices(rows.length, spec.maxTicks)}
          tickFormatter={(index: number) => spec.labels[index] ?? ""}
          interval={0}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={AXIS_LINE}
        />
        <YAxis
          width={Y_AXIS_WIDTH}
          domain={[0, scale.max]}
          ticks={scale.ticks}
          tickFormatter={(value: number) => formatAxisNumber(value, scale.max)}
          interval={0}
          allowDecimals={false}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
        />
        <ChartTooltip
          content={<ChartTooltipContent tips={spec.tips} />}
          cursor={CURSOR_LINE}
          isAnimationActive={false}
        />
        {hasLimit ? (
          <Line
            dataKey={LINE_LIMIT_KEY}
            stroke={limitStyle.color}
            strokeWidth={LINE_STROKE_WIDTH}
            strokeDasharray={limitStyle.dash}
            dot={(dot) =>
              dot.index === lastIndex ? markerAt(dot, limitStyle) : null
            }
            activeDot={false}
            isAnimationActive={false}
          >
            <LabelList
              valueAccessor={(_, index) =>
                index === lastIndex ? spec.referenceLabel : null
              }
              position="insideBottomRight"
              offset={LIMIT_LABEL_OFFSET}
              fill={LABEL_FILL}
              fontSize={AXIS_TICK.fontSize}
            />
          </Line>
        ) : null}
        <Line
          dataKey={LINE_VALUE_KEY}
          stroke={lineStyle.color}
          strokeWidth={LINE_STROKE_WIDTH}
          strokeDasharray={lineStyle.dash}
          connectNulls={false}
          dot={(dot) =>
            valueMarkers.has(dot.index) ? markerAt(dot, lineStyle) : null
          }
          activeDot={false}
          isAnimationActive={false}
        />
      </RechartsLineChart>
    </ChartContainer>
  );
}
