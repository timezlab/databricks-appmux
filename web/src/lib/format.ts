/**
 * Display formats for the admin UI: pure functions, no `Intl`, no locale and no
 * machine time zone, so the same input prints the same text in every browser,
 * in the unit tests and in the static prerender.
 *
 * Times are UTC and say so, because the API, the audit log and the logs an
 * admin compares them with are UTC (admin-api.md, Conventions).
 */

/** What a formatter prints for a value that is not a valid input. */
export const UNKNOWN_TEXT = "unknown";

/** An ISO 8601 UTC string of the contract (`2026-10-30T09:14:00Z`) or epoch milliseconds. */
export type TimeInput = string | number;

const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE;
const SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR;

/** Within this distance of now, in either direction, a time reads "just now" (the client's clock is not the server's). */
const JUST_NOW_MS = 10 * SECOND_MS;
/** From this distance on, a relative time becomes a date. */
const RELATIVE_LIMIT_MS = 30 * DAY_MS;

const BYTES_PER_UNIT = 1024;
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;
const MILLISECONDS_PER_TENTH_SECOND = 100;
const TENTHS_PER_SECOND = 10;
const TENTHS_BELOW_TEN_SECONDS = 100;
const PERCENT = 100;

// Zod's z.iso.datetime(): date, time with seconds, optional fraction, "Z".
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;
const DATE_TIME_PREFIX_LENGTH = "2026-10-30T09:14:00".length;
const THOUSANDS_GROUP = /\B(?=(\d{3})+(?!\d))/g;

// ------------------------------------------------------------------- time

/**
 * The range of the contract's text form, whose year has four digits:
 * `0000-01-01T00:00:00Z` to `9999-12-31T23:59:59.999Z`. A number outside it is
 * not a time: past ±8.64e15 a `Date` is invalid and every formatter would
 * throw or print NaN, and in between, `toISOString` would write a year with a
 * sign and six digits that no `dateTime` attribute accepts.
 */
const FIRST_TIME_MS = -62_167_219_200_000;
const LAST_TIME_MS = 253_402_300_799_999;

/** Epoch milliseconds, or `null` when `value` is not a time of the contract. */
export function parseTime(value: TimeInput): number | null {
  if (typeof value === "number") {
    return value >= FIRST_TIME_MS && value <= LAST_TIME_MS ? value : null;
  }
  if (!UTC_TIME.test(value)) return null;
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) return null;
  // Date.parse rolls "2026-02-30" over to March and reads "24:00:00" as the
  // next day; a date that does not exist is a wrong value, not a different one.
  const written = value.slice(0, DATE_TIME_PREFIX_LENGTH);
  const read = new Date(ms).toISOString().slice(0, DATE_TIME_PREFIX_LENGTH);
  return written === read ? ms : null;
}

/** `2026-10-30T09:14:00.000Z`, or `null` when `value` is not a time. */
export function toIsoUtc(value: TimeInput): string | null {
  const ms = parseTime(value);
  return ms === null ? null : new Date(ms).toISOString();
}

function twoDigits(value: number): string {
  return String(value).padStart(2, "0");
}

function utcDate(date: Date): string {
  return `${date.getUTCFullYear()}-${twoDigits(date.getUTCMonth() + 1)}-${twoDigits(date.getUTCDate())}`;
}

/** `2026-10-30` (UTC). */
export function formatDate(value: TimeInput): string {
  const ms = parseTime(value);
  return ms === null ? UNKNOWN_TEXT : utcDate(new Date(ms));
}

/** `2026-10-30 09:14 UTC`, or with `seconds`, `2026-10-30 09:14:00 UTC`. */
export function formatAbsoluteTime(
  value: TimeInput,
  options: { seconds?: boolean } = {},
): string {
  const ms = parseTime(value);
  if (ms === null) return UNKNOWN_TEXT;
  const date = new Date(ms);
  const clock = `${twoDigits(date.getUTCHours())}:${twoDigits(date.getUTCMinutes())}`;
  const withSeconds = options.seconds
    ? `${clock}:${twoDigits(date.getUTCSeconds())}`
    : clock;
  return `${utcDate(date)} ${withSeconds} UTC`;
}

function countedUnit(count: number, unit: string): string {
  return `${count} ${unit}${count === 1 ? "" : "s"}`;
}

/**
 * "just now", "3 minutes ago", "in 2 hours", rounded down; from 30 days on, the
 * date. `nowMs` is a parameter so the result is a pure function of its inputs;
 * pages get it from `RelativeTime` (src/components), not from `Date.now()`.
 */
export function formatRelativeTime(value: TimeInput, nowMs: number): string {
  const ms = parseTime(value);
  if (ms === null) return UNKNOWN_TEXT;
  const distance = Math.abs(nowMs - ms);
  if (distance < JUST_NOW_MS) return "just now";
  if (distance >= RELATIVE_LIMIT_MS) return formatDate(ms);

  let amount: string;
  if (distance < MINUTE_MS) {
    amount = countedUnit(Math.floor(distance / SECOND_MS), "second");
  } else if (distance < HOUR_MS) {
    amount = countedUnit(Math.floor(distance / MINUTE_MS), "minute");
  } else if (distance < DAY_MS) {
    amount = countedUnit(Math.floor(distance / HOUR_MS), "hour");
  } else {
    amount = countedUnit(Math.floor(distance / DAY_MS), "day");
  }
  return ms <= nowMs ? `${amount} ago` : `in ${amount}`;
}

/** What `RelativeTime` draws for one time. */
export interface TimeDescription {
  /** `2026-10-30T09:14:00.000Z`, for the `dateTime` attribute. */
  iso: string;
  /** `2026-10-30 09:14 UTC`, for the hover title. */
  absolute: string;
  /** The shown text: relative to `nowMs`, or the absolute time when there is no `nowMs`. */
  text: string;
}

/**
 * `null` when there is nothing to describe (no value, or not a time): the
 * caller draws `UNKNOWN_TEXT`. `nowMs` is the clock of the moment of rendering,
 * never an older reading: with one older than the value, a time that has just
 * happened would read "in N seconds". `null` (the static prerender, which has
 * no "now") gives the absolute time.
 */
export function describeTime(
  value: TimeInput | null | undefined,
  nowMs: number | null,
): TimeDescription | null {
  if (value === null || value === undefined) return null;
  const iso = toIsoUtc(value);
  if (iso === null) return null;
  const absolute = formatAbsoluteTime(value);
  return {
    iso,
    absolute,
    text: nowMs === null ? absolute : formatRelativeTime(value, nowMs),
  };
}

// ---------------------------------------------------------------- numbers

function isNonNegativeFinite(value: number): boolean {
  return Number.isFinite(value) && value >= 0;
}

/** `1234567` as `1,234,567`. A fraction is rounded; a non-number is `unknown`. */
export function formatCount(value: number): string {
  if (!Number.isFinite(value)) return UNKNOWN_TEXT;
  const rounded = Math.round(value);
  const digits = String(Math.abs(rounded)).replace(THOUSANDS_GROUP, ",");
  return rounded < 0 ? `-${digits}` : digits;
}

/** "1 release", "3 releases"; pass `plural` when adding an "s" is wrong. */
export function pluralize(
  count: number,
  singular: string,
  plural = `${singular}s`,
): string {
  return `${formatCount(count)} ${count === 1 ? singular : plural}`;
}

/** `part` over `whole` as a whole percent (`81%`); `unknown` when there is no whole. */
export function formatPercent(part: number, whole: number): string {
  if (!Number.isFinite(part) || !Number.isFinite(whole) || whole <= 0) {
    return UNKNOWN_TEXT;
  }
  return `${Math.round((part / whole) * PERCENT)}%`;
}

/** One decimal under 10 (`1.5`), none from 10 on; a trailing `.0` is dropped. */
function roundForDisplay(value: number): string {
  if (value >= TENTHS_PER_SECOND) return formatCount(value);
  return String(Number(value.toFixed(1)));
}

/**
 * `1536` as `1.5 KB`. A unit is 1024 of the one before, like the memory
 * figures of the design (`memory_budget_mb` 1024, "7,168 MB"), so the same
 * number reads the same on every screen.
 */
export function formatBytes(bytes: number): string {
  if (!isNonNegativeFinite(bytes)) return UNKNOWN_TEXT;
  let value = bytes;
  let unit = 0;
  while (value >= BYTES_PER_UNIT && unit < BYTE_UNITS.length - 1) {
    value /= BYTES_PER_UNIT;
    unit += 1;
  }
  let text = unit === 0 ? String(Math.round(value)) : roundForDisplay(value);
  // 1023.9 KB prints as "1024 KB"; that is "1 MB".
  if (Number(text.replaceAll(",", "")) >= BYTES_PER_UNIT) {
    if (unit < BYTE_UNITS.length - 1) {
      value /= BYTES_PER_UNIT;
      unit += 1;
      text = roundForDisplay(value);
    }
  }
  return `${text} ${BYTE_UNITS[unit]}`;
}

/** A size already in megabytes (`rss_mb_max`, `memory_budget_mb`): `5,775 MB`. */
export function formatMegabytes(megabytes: number): string {
  if (!isNonNegativeFinite(megabytes)) return UNKNOWN_TEXT;
  return `${formatCount(megabytes)} MB`;
}

/**
 * Milliseconds as the two largest units: `840 ms`, `1.2 s`, `12 s`,
 * `3 min 5 s`, `2 h 10 min`, `1 d 4 h`.
 */
export function formatDurationMs(ms: number): string {
  if (!isNonNegativeFinite(ms)) return UNKNOWN_TEXT;
  const rounded = Math.round(ms);
  if (rounded < SECOND_MS) return `${rounded} ms`;

  const tenths = Math.round(ms / MILLISECONDS_PER_TENTH_SECOND);
  if (tenths < TENTHS_BELOW_TEN_SECONDS) {
    return `${tenths / TENTHS_PER_SECOND} s`;
  }

  const totalSeconds = Math.round(ms / SECOND_MS);
  if (totalSeconds < SECONDS_PER_MINUTE) return `${totalSeconds} s`;
  if (totalSeconds < SECONDS_PER_HOUR) {
    return joinUnits(
      Math.floor(totalSeconds / SECONDS_PER_MINUTE),
      "min",
      totalSeconds % SECONDS_PER_MINUTE,
      "s",
    );
  }
  if (totalSeconds < SECONDS_PER_DAY) {
    return joinUnits(
      Math.floor(totalSeconds / SECONDS_PER_HOUR),
      "h",
      Math.floor((totalSeconds % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE),
      "min",
    );
  }
  return joinUnits(
    Math.floor(totalSeconds / SECONDS_PER_DAY),
    "d",
    Math.floor((totalSeconds % SECONDS_PER_DAY) / SECONDS_PER_HOUR),
    "h",
  );
}

function joinUnits(
  larger: number,
  largerUnit: string,
  smaller: number,
  smallerUnit: string,
): string {
  return smaller === 0
    ? `${larger} ${largerUnit}`
    : `${larger} ${largerUnit} ${smaller} ${smallerUnit}`;
}
