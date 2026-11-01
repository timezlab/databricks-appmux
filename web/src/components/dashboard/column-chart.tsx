import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ReferenceArea,
  XAxis,
  YAxis,
} from "recharts";
import type { BarShapeProps } from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import { formatAxisNumber, xTickIndices } from "@/lib/dashboard/chart";
import {
  HIGHLIGHT_KEY,
  columnAxis,
  columnRows,
  gapRuns,
  segmentKey,
} from "@/lib/dashboard/chart-data";
import type { ColumnChartSpec } from "@/lib/dashboard/model";
import {
  AXIS_LINE,
  AXIS_TICK,
  CHART_SIZE_CLASS,
  CURSOR_BAND,
  GAP_FILL,
  GRID_STROKE,
  LABEL_FILL,
  PLOT_MARGIN,
  Y_AXIS_WIDTH,
} from "./chart-theme";
import { TexturePattern, barFill, textureId } from "./series-marks";

/** A bar never grows wider than this, however few columns there are (a 5-column latency chart). */
const MAX_BAR_WIDTH = 32;
const BAR_CATEGORY_GAP = "15%";
const HIGHLIGHT_LABEL_OFFSET = 6;
/** In pixels. Three server errors among 100,000 requests are far less than one, and must still show. */
const MIN_SEGMENT_HEIGHT = 1;

/**
 * A rectangle for a bar segment that is never flatter than a pixel, growing
 * upward from its base. Recharts' own `minPointSize` is documented as unreliable
 * in a stack, and a segment that vanishes would hide exactly the errors this
 * chart is there to show.
 */
function segmentShape({ x, y, width, height, fill }: BarShapeProps) {
  if (x === undefined || y === undefined || width === undefined) return null;
  // Height 0 is a segment with no value (`columnRows` makes zero and no data `null`).
  if (height === undefined || height <= 0) return null;
  const shownHeight = Math.max(height, MIN_SEGMENT_HEIGHT);
  return (
    <rect
      x={x}
      y={y + height - shownHeight}
      width={width}
      height={shownHeight}
      fill={fill}
    />
  );
}

/**
 * Stacked or single columns: one `Bar` per segment of `spec.tones`, bottom
 * first, over the shared category axis (the column's index, written as its
 * time label). Stretches of minutes with no data are shaded.
 */
export function ColumnChart({ spec }: { spec: ColumnChartSpec }) {
  const rows = columnRows(spec);
  const scale = columnAxis(spec);
  const idPrefix = `texture-${spec.id}`;
  const topSegment = spec.tones.length - 1;
  return (
    <ChartContainer
      label={spec.title}
      description={spec.description}
      className={CHART_SIZE_CLASS}
    >
      <BarChart
        data={rows}
        margin={PLOT_MARGIN}
        accessibilityLayer={false}
        barCategoryGap={BAR_CATEGORY_GAP}
        maxBarSize={MAX_BAR_WIDTH}
      >
        <defs>
          {spec.tones.map((tone) => (
            <TexturePattern
              key={tone}
              id={textureId(idPrefix, tone)}
              tone={tone}
            />
          ))}
        </defs>
        <CartesianGrid vertical={false} stroke={GRID_STROKE} />
        {gapRuns(spec.columns).map((run) => (
          <ReferenceArea
            key={run.first}
            x1={run.first}
            x2={run.last}
            fill={GAP_FILL}
            stroke="none"
          />
        ))}
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
          cursor={CURSOR_BAND}
          isAnimationActive={false}
        />
        {spec.tones.map((tone, segment) => (
          <Bar
            key={segmentKey(segment)}
            dataKey={segmentKey(segment)}
            stackId="stack"
            fill={barFill(textureId(idPrefix, tone), tone)}
            shape={segmentShape}
            isAnimationActive={false}
          >
            {segment === topSegment && spec.highlights.length > 0 ? (
              <LabelList
                dataKey={HIGHLIGHT_KEY}
                position="top"
                offset={HIGHLIGHT_LABEL_OFFSET}
                fill={LABEL_FILL}
                fontSize={AXIS_TICK.fontSize}
              />
            ) : null}
          </Bar>
        ))}
      </BarChart>
    </ChartContainer>
  );
}
