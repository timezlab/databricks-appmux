/**
 * The last branch of an exhaustive `switch` or `if` chain: the compiler accepts
 * it only while every case is handled (the argument is then `never`), and a
 * value that still arrives at run time, say a new variant from a newer server,
 * throws with its content instead of falling through silently.
 */
export function assertNever(value: never): never {
  throw new Error(`Unhandled value ${JSON.stringify(value)}.`);
}
