/**
 * How different two colours look, to a person with normal colour vision or
 * with one of the three dichromacies, for the test that checks the chart
 * tones of `src/app/globals.css`. Pure and dependency-free; the page itself
 * never calls it.
 *
 * - Simulation: Machado, Oliveira and Fernandes (2009), "A Physiologically-based
 *   Model for Simulation of Color Vision Deficiency", IEEE TVCG 15(6), the
 *   matrices for severity 1.0, applied to linear sRGB.
 * - CIELAB: sRGB (IEC 61966-2-1) to CIE XYZ to L*a*b*, D65 white.
 * - CIEDE2000: Sharma, Wu and Dalal (2005), "The CIEDE2000 Color-Difference
 *   Formula: Implementation Notes, Supplementary Test Data, and Mathematical
 *   Observations", Color Research and Application 30(1).
 */
import type { Rgb } from "./contrast";

export type VisionCondition =
  "normal" | "deuteranopia" | "protanopia" | "tritanopia";

export const VISION_CONDITIONS: readonly VisionCondition[] = [
  "normal",
  "deuteranopia",
  "protanopia",
  "tritanopia",
];

/** L* (0 black to 100 white), a* (green to red), b* (blue to yellow). */
export type Lab = readonly [lightness: number, a: number, b: number];

type Row = readonly [number, number, number];
type Matrix = readonly [Row, Row, Row];

const IDENTITY: Matrix = [
  [1, 0, 0],
  [0, 1, 0],
  [0, 0, 1],
];

// Machado 2009, table of severity 1.0. Each row sums to 1, so a grey stays grey.
const SIMULATION: Record<VisionCondition, Matrix> = {
  normal: IDENTITY,
  deuteranopia: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
  protanopia: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  tritanopia: [
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930809, 0.147602],
    [0.004733, 0.691367, 0.3039],
  ],
};

// IEC 61966-2-1: linear sRGB to XYZ, and the D65 white it is relative to.
const SRGB_TO_XYZ: Matrix = [
  [0.4124564, 0.3575761, 0.1804375],
  [0.2126729, 0.7151522, 0.072175],
  [0.0193339, 0.119192, 0.9503041],
];
const D65_WHITE: Row = [0.95047, 1, 1.08883];

const CHANNEL_MAX = 255;
const SRGB_LINEAR_SEGMENT_END = 0.04045;
const LAB_EPSILON = 216 / 24389;
const LAB_KAPPA = 24389 / 27;

function linearChannel(channel: number): number {
  const fraction = channel / CHANNEL_MAX;
  return fraction <= SRGB_LINEAR_SEGMENT_END
    ? fraction / 12.92
    : ((fraction + 0.055) / 1.055) ** 2.4;
}

function times(matrix: Matrix, [x, y, z]: Row): Row {
  const [first, second, third] = matrix.map(
    ([m0, m1, m2]) => m0 * x + m1 * y + m2 * z,
  );
  if (first === undefined || second === undefined || third === undefined) {
    throw new Error("a 3 by 3 matrix gives three values");
  }
  return [first, second, third];
}

function clampUnit(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function labPart(ratio: number): number {
  return ratio > LAB_EPSILON
    ? Math.cbrt(ratio)
    : (LAB_KAPPA * ratio + 16) / 116;
}

/** The colour in CIELAB as a person with `condition` sees it (a simulated colour outside sRGB is clipped). */
export function labAsSeen(
  [red, green, blue]: Rgb,
  condition: VisionCondition,
): Lab {
  const linear: Row = [
    linearChannel(red),
    linearChannel(green),
    linearChannel(blue),
  ];
  const [r, g, b] = times(SIMULATION[condition], linear);
  const [x, y, z] = times(SRGB_TO_XYZ, [
    clampUnit(r),
    clampUnit(g),
    clampUnit(b),
  ]);
  const fx = labPart(x / D65_WHITE[0]);
  const fy = labPart(y / D65_WHITE[1]);
  const fz = labPart(z / D65_WHITE[2]);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

const DEGREES_PER_RADIAN = 180 / Math.PI;
const TWENTY_FIVE_TO_THE_SEVENTH = 25 ** 7;

function sinDegrees(degrees: number): number {
  return Math.sin(degrees / DEGREES_PER_RADIAN);
}

function cosDegrees(degrees: number): number {
  return Math.cos(degrees / DEGREES_PER_RADIAN);
}

/** 0 to 360, and 0 for a grey (Sharma 2005, equation 7). */
function hueDegrees(b: number, aPrime: number): number {
  if (b === 0 && aPrime === 0) return 0;
  const degrees = Math.atan2(b, aPrime) * DEGREES_PER_RADIAN;
  return degrees < 0 ? degrees + 360 : degrees;
}

/** CIEDE2000 with kL = kC = kH = 1: about 1 is a just-noticeable difference. */
export function ciede2000(
  [lightness1, a1, b1]: Lab,
  [lightness2, a2, b2]: Lab,
): number {
  const chromaMean = (Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2;
  const g =
    0.5 *
    (1 -
      Math.sqrt(
        chromaMean ** 7 / (chromaMean ** 7 + TWENTY_FIVE_TO_THE_SEVENTH),
      ));
  const a1Prime = (1 + g) * a1;
  const a2Prime = (1 + g) * a2;
  const chroma1 = Math.hypot(a1Prime, b1);
  const chroma2 = Math.hypot(a2Prime, b2);
  const hue1 = hueDegrees(b1, a1Prime);
  const hue2 = hueDegrees(b2, a2Prime);
  const isGrey = chroma1 * chroma2 === 0;

  const deltaLightness = lightness2 - lightness1;
  const deltaChroma = chroma2 - chroma1;
  let deltaHueAngle = hue2 - hue1;
  if (isGrey) deltaHueAngle = 0;
  else if (deltaHueAngle > 180) deltaHueAngle -= 360;
  else if (deltaHueAngle < -180) deltaHueAngle += 360;
  const deltaHue =
    2 * Math.sqrt(chroma1 * chroma2) * sinDegrees(deltaHueAngle / 2);

  const lightnessMean = (lightness1 + lightness2) / 2;
  const chromaPrimeMean = (chroma1 + chroma2) / 2;
  let hueMean = hue1 + hue2;
  if (!isGrey) {
    if (Math.abs(hue1 - hue2) <= 180) hueMean /= 2;
    else hueMean = (hueMean + (hueMean < 360 ? 360 : -360)) / 2;
  }

  const t =
    1 -
    0.17 * cosDegrees(hueMean - 30) +
    0.24 * cosDegrees(2 * hueMean) +
    0.32 * cosDegrees(3 * hueMean + 6) -
    0.2 * cosDegrees(4 * hueMean - 63);
  const deltaTheta = 30 * Math.exp(-(((hueMean - 275) / 25) ** 2));
  const rotationChroma =
    2 *
    Math.sqrt(
      chromaPrimeMean ** 7 /
        (chromaPrimeMean ** 7 + TWENTY_FIVE_TO_THE_SEVENTH),
    );
  const lightnessOffset = (lightnessMean - 50) ** 2;
  const weightLightness =
    1 + (0.015 * lightnessOffset) / Math.sqrt(20 + lightnessOffset);
  const weightChroma = 1 + 0.045 * chromaPrimeMean;
  const weightHue = 1 + 0.015 * chromaPrimeMean * t;
  const rotation = -sinDegrees(2 * deltaTheta) * rotationChroma;

  const lightnessTerm = deltaLightness / weightLightness;
  const chromaTerm = deltaChroma / weightChroma;
  const hueTerm = deltaHue / weightHue;
  return Math.sqrt(
    lightnessTerm ** 2 +
      chromaTerm ** 2 +
      hueTerm ** 2 +
      rotation * chromaTerm * hueTerm,
  );
}
