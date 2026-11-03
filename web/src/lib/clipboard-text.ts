/**
 * What a Copy button may put on the clipboard. A child's output (its log
 * lines, its variables) is text the app wrote, and it can hold an ESC: pasted
 * into a terminal, `ESC [` and `ESC ]` sequences move the cursor, retitle the
 * window or end bracketed paste so the rest runs as typed commands. Some
 * terminals read the single C1 characters (U+009B is `ESC [`, U+009D is
 * `ESC ]`) the same way. So every C0 and C1 control character is dropped
 * except the three that shape plain text: tab, line feed and carriage return
 * (alone, as a progress bar writes it, or before a line feed).
 */
const TAB = 0x09;
const LINE_FEED = 0x0a;
const CARRIAGE_RETURN = 0x0d;
const LAST_C0 = 0x1f;
const FIRST_C1 = 0x80;
const LAST_C1 = 0x9f;

function isDroppedControl(codeUnit: number): boolean {
  if (codeUnit >= FIRST_C1 && codeUnit <= LAST_C1) return true;
  return (
    codeUnit <= LAST_C0 &&
    codeUnit !== TAB &&
    codeUnit !== LINE_FEED &&
    codeUnit !== CARRIAGE_RETURN
  );
}

export function clipboardText(text: string): string {
  let kept = "";
  for (const character of text) {
    if (!isDroppedControl(character.charCodeAt(0))) kept += character;
  }
  return kept;
}
