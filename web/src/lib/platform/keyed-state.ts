/**
 * State that starts over when its key changes (see `useKeyedState`). A form on
 * the platform page shows values from the polled data: while those values stay
 * the same, what the person typed stays; when they change (another admin
 * saved), the form takes the new ones. The stored value is tagged with the key
 * it was written under, and a value written under another key reads as the
 * initial one, so no effect is needed to reset it.
 */
export interface Keyed<T> {
  key: string;
  value: T;
}

/** What the state holds for `key`: the written value, or `initial` when it was written under another key. */
export function keyedValue<T>(held: Keyed<T>, key: string, initial: T): T {
  return held.key === key ? held.value : initial;
}

/** The state after a write; an updater function receives what `keyedValue` would read. */
export function writeKeyed<T>(
  held: Keyed<T>,
  key: string,
  initial: T,
  next: T | ((previous: T) => T),
): Keyed<T> {
  const previous = keyedValue(held, key, initial);
  return {
    key,
    value:
      typeof next === "function" ? (next as (value: T) => T)(previous) : next,
  };
}
