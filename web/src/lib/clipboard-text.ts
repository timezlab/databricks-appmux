/**
 * What a Copy button may put on the clipboard. A child's output (its log
 * lines, its variables) is text the app wrote, and the clipboard hands it to
 * whatever it is pasted into, often a terminal. So these are dropped:
 *
 * - every C0 control character (U+0000 to U+001F) except the three that shape
 *   plain text: tab, line feed and carriage return (alone, as a progress bar
 *   writes it, or before a line feed). An ESC starts the `ESC [` and `ESC ]`
 *   sequences that move the cursor, retitle the window or end bracketed paste
 *   so the rest runs as typed commands;
 * - DEL (U+007F): a shell without bracketed paste reads it as a backspace, so
 *   the line that runs is shorter than the line that was shown;
 * - the C1 control characters (U+0080 to U+009F): some terminals read the
 *   single U+009B as `ESC [` and U+009D as `ESC ]`;
 * - the explicit bidirectional embeddings, overrides and isolates (U+202A to
 *   U+202E and U+2066 to U+2069): they reorder what a person sees, so a pasted
 *   command can read differently from what runs.
 *
 * Everything else is kept, including the implicit direction marks U+200E and
 * U+200F (real Arabic and Hebrew text uses them), the line and paragraph
 * separators U+2028 and U+2029, the zero-width joiner of emoji sequences and
 * the tag characters (U+E0000 block) inside emoji flag sequences.
 */
const TAB = 0x09;
const LINE_FEED = 0x0a;
const CARRIAGE_RETURN = 0x0d;
const LAST_C0 = 0x1f;
const DELETE = 0x7f;
const FIRST_C1 = 0x80;
const LAST_C1 = 0x9f;
const FIRST_BIDI_EMBEDDING = 0x202a;
const LAST_BIDI_EMBEDDING = 0x202e;
const FIRST_BIDI_ISOLATE = 0x2066;
const LAST_BIDI_ISOLATE = 0x2069;

function isDroppedCodePoint(codePoint: number): boolean {
  if (codePoint === DELETE) return true;
  if (codePoint >= FIRST_C1 && codePoint <= LAST_C1) return true;
  if (codePoint >= FIRST_BIDI_EMBEDDING && codePoint <= LAST_BIDI_EMBEDDING) {
    return true;
  }
  if (codePoint >= FIRST_BIDI_ISOLATE && codePoint <= LAST_BIDI_ISOLATE) {
    return true;
  }
  return (
    codePoint <= LAST_C0 &&
    codePoint !== TAB &&
    codePoint !== LINE_FEED &&
    codePoint !== CARRIAGE_RETURN
  );
}

export function clipboardText(text: string): string {
  let kept = "";
  for (const character of text) {
    const codePoint = character.codePointAt(0);
    if (codePoint === undefined || !isDroppedCodePoint(codePoint)) {
      kept += character;
    }
  }
  return kept;
}
