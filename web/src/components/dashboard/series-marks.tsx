import type { ChartTone, ToneTexture } from "@/lib/dashboard/model";
import { seriesStyle } from "@/lib/dashboard/series-style";
import type { MarkerShape } from "@/lib/dashboard/series-style";
import { MARKER_RING } from "./chart-theme";

/** Side of the repeating tile of a texture, in the chart's user units. */
const TEXTURE_TILE = 6;
const STRIPE_WIDTH = 3;
/** The two cut lines of a crosshatch are thinner than a stripe, so as much colour shows. */
const CROSSHATCH_LINE_WIDTH = 1.5;
const DOT_RADIUS = 1.25;

/** Stripes and the crosshatch are drawn upright in the tile, then the tile is turned. */
const TILE_TURN: Record<Exclude<ToneTexture, "none">, string | undefined> = {
  "rising-stripes": "rotate(45)",
  "falling-stripes": "rotate(-45)",
  crosshatch: "rotate(45)",
  dots: undefined,
};
const MARKER_RADIUS = 4;
const MARKER_RING_WIDTH = 2;

/** The `id` of the pattern a bar of `tone` is filled with. */
export function textureId(prefix: string, tone: ChartTone): string {
  return `${prefix}-${tone}`;
}

/** The marks a texture cuts out of its colour, in the background colour, drawn upright in one tile. */
function TextureMarks({ texture }: { texture: Exclude<ToneTexture, "none"> }) {
  switch (texture) {
    case "rising-stripes":
    case "falling-stripes":
      return (
        <rect width={STRIPE_WIDTH} height={TEXTURE_TILE} fill={MARKER_RING} />
      );
    case "crosshatch":
      return (
        <>
          <rect
            width={CROSSHATCH_LINE_WIDTH}
            height={TEXTURE_TILE}
            fill={MARKER_RING}
          />
          <rect
            width={TEXTURE_TILE}
            height={CROSSHATCH_LINE_WIDTH}
            fill={MARKER_RING}
          />
        </>
      );
    case "dots":
      return (
        <circle
          cx={TEXTURE_TILE / 2}
          cy={TEXTURE_TILE / 2}
          r={DOT_RADIUS}
          fill={MARKER_RING}
        />
      );
  }
}

/**
 * The `<pattern>` of a tone's texture: its colour, with the marks cut out in
 * the background colour. Nothing for a plain tone. Goes inside `<defs>`.
 */
export function TexturePattern({ id, tone }: { id: string; tone: ChartTone }) {
  const { color, texture } = seriesStyle(tone);
  if (texture === "none") return null;
  return (
    <pattern
      id={id}
      patternUnits="userSpaceOnUse"
      width={TEXTURE_TILE}
      height={TEXTURE_TILE}
      patternTransform={TILE_TURN[texture]}
    >
      <rect width={TEXTURE_TILE} height={TEXTURE_TILE} fill={color} />
      <TextureMarks texture={texture} />
    </pattern>
  );
}

/** The `fill` of a bar of `tone`: its colour, or its pattern when it has one. */
export function barFill(id: string, tone: ChartTone): string {
  const { color, texture } = seriesStyle(tone);
  return texture === "none" ? color : `url(#${id})`;
}

export interface MarkerProps {
  shape: MarkerShape;
  x: number;
  y: number;
  color: string;
  /** The `fill-opacity` of a partial bucket's point (`markOpacity`); none draws it at full strength. */
  fillOpacity?: number | undefined;
}

/** A point's marker: a circle, a square or a diamond, ringed in the background colour. */
export function Marker({ shape, x, y, color, fillOpacity }: MarkerProps) {
  const common = {
    fill: color,
    fillOpacity,
    stroke: MARKER_RING,
    strokeWidth: MARKER_RING_WIDTH,
  };
  switch (shape) {
    case "circle":
      return <circle cx={x} cy={y} r={MARKER_RADIUS} {...common} />;
    case "square":
      return (
        <rect
          x={x - MARKER_RADIUS}
          y={y - MARKER_RADIUS}
          width={MARKER_RADIUS * 2}
          height={MARKER_RADIUS * 2}
          {...common}
        />
      );
    case "diamond": {
      const reach = MARKER_RADIUS * 1.4;
      return (
        <polygon
          points={`${x},${y - reach} ${x + reach},${y} ${x},${y + reach} ${x - reach},${y}`}
          {...common}
        />
      );
    }
  }
}
