export interface LiveRegionProps {
  /** The sentence to announce ("Saved sales"); `null` for nothing. Plain text. */
  message: string | null;
}

/**
 * An always-present, invisible spot that a screen reader watches. A message
 * put into an element that already exists is announced reliably; an element
 * that appears together with its text is not, in every reader. Use it for the
 * outcome of an action; show the same outcome on screen with a `Notice`.
 */
export function LiveRegion({ message }: LiveRegionProps) {
  return (
    <div
      className="sr-only"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      {message}
    </div>
  );
}
