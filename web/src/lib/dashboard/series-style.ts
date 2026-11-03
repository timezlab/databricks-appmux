/**
 * How each tone of a chart looks, beyond its colour. The colours are the fixed
 * series tokens of globals.css (`--chart-1..6`, each checked for contrast
 * against both backgrounds in tests/contract/contrast.test.ts), written into
 * the SVG as presentation attributes: the strict CSP allows no style attribute.
 * Colour alone never tells two series apart, so every tone also has a texture
 * for bars (`TONE_TEXTURE`), a dash pattern for lines and a marker shape, and
 * the legend names it.
 */
import { TONE_TEXTURE } from "./model";
import type { ChartTone, ToneTexture } from "./model";

export type MarkerShape = "circle" | "square" | "diamond";

export interface SeriesStyle {
  /** A CSS value for the `fill` and `stroke` attributes: `var(--chart-N)`. */
  color: string;
  /** Pattern over a bar; the legend swatch repeats it. */
  texture: ToneTexture;
  /** `stroke-dasharray` of a line, `undefined` for a solid one. */
  dash: string | undefined;
  marker: MarkerShape;
}

const STYLE_BY_TONE: Record<
  ChartTone,
  Pick<SeriesStyle, "color" | "dash" | "marker">
> = {
  primary: { color: "var(--chart-1)", dash: undefined, marker: "circle" },
  warning: { color: "var(--chart-5)", dash: "2 3", marker: "square" },
  danger: { color: "var(--chart-2)", dash: "6 4", marker: "diamond" },
  // The apps of the breakdown, in this fixed order (never cycled). They are
  // drawn as stacked columns; the dash and marker serve a line, should one
  // ever show an app.
  "series-1": { color: "var(--chart-1)", dash: undefined, marker: "circle" },
  "series-2": { color: "var(--chart-2)", dash: "6 4", marker: "diamond" },
  "series-3": { color: "var(--chart-3)", dash: undefined, marker: "square" },
  "series-4": { color: "var(--chart-4)", dash: "6 4", marker: "circle" },
  "series-5": { color: "var(--chart-5)", dash: undefined, marker: "diamond" },
  "series-6": { color: "var(--chart-6)", dash: "6 4", marker: "square" },
  "series-7": { color: "var(--chart-1)", dash: "2 3", marker: "diamond" },
  "series-8": { color: "var(--chart-2)", dash: undefined, marker: "square" },
  // Every app below the cut: neutral, so it never reads as one more app.
  other: { color: "var(--muted-foreground)", dash: "2 3", marker: "circle" },
};

export function seriesStyle(tone: ChartTone): SeriesStyle {
  return { ...STYLE_BY_TONE[tone], texture: TONE_TEXTURE[tone] };
}
