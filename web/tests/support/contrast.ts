/**
 * WCAG 2.x colour contrast, for the test that checks the theme tokens of
 * `src/app/globals.css`. Pure and dependency-free; the page itself never calls it.
 * Definitions: https://www.w3.org/TR/WCAG22/#dfn-contrast-ratio and
 * https://www.w3.org/TR/WCAG22/#dfn-relative-luminance
 */

/** Red, green and blue as integers 0 to 255. */
export type Rgb = readonly [red: number, green: number, blue: number];

/** Success criterion 1.4.3 (AA): text smaller than 18.66 px bold or 24 px. */
export const WCAG_AA_NORMAL_TEXT_RATIO = 4.5;
/** Success criterion 1.4.11 (AA): control boundaries, focus indicators, graphics. */
export const WCAG_AA_NON_TEXT_RATIO = 3;

const HEX_COLOUR = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;
const CHANNEL_MAX = 255;
// Below this the sRGB transfer curve is linear; the constant is the one WCAG prints.
const LINEAR_SEGMENT_END = 0.03928;
const LUMINANCE_OFFSET = 0.05;

/** `#rgb` or `#rrggbb`. Anything else throws, so a token that is not plain hex cannot slip past the check. */
export function parseHexColor(hex: string): Rgb {
  const match = HEX_COLOUR.exec(hex);
  const digits = match?.[1];
  if (digits === undefined) {
    throw new Error(
      `Not a hex colour (#rgb or #rrggbb): ${JSON.stringify(hex)}`,
    );
  }
  const full =
    digits.length === 3
      ? [...digits].map((digit) => digit + digit).join("")
      : digits;
  return [
    Number.parseInt(full.slice(0, 2), 16),
    Number.parseInt(full.slice(2, 4), 16),
    Number.parseInt(full.slice(4, 6), 16),
  ];
}

function linearChannel(channel: number): number {
  const fraction = channel / CHANNEL_MAX;
  return fraction <= LINEAR_SEGMENT_END
    ? fraction / 12.92
    : ((fraction + 0.055) / 1.055) ** 2.4;
}

/** 0 for black, 1 for white. */
export function relativeLuminance([red, green, blue]: Rgb): number {
  return (
    0.2126 * linearChannel(red) +
    0.7152 * linearChannel(green) +
    0.0722 * linearChannel(blue)
  );
}

/** 1 (identical) to 21 (black on white), whichever of the two is the lighter. */
export function contrastRatio(firstHex: string, secondHex: string): number {
  const first = relativeLuminance(parseHexColor(firstHex));
  const second = relativeLuminance(parseHexColor(secondHex));
  const lighter = Math.max(first, second);
  const darker = Math.min(first, second);
  return (lighter + LUMINANCE_OFFSET) / (darker + LUMINANCE_OFFSET);
}
