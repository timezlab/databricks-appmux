"use client";

import { useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { keyedValue, writeKeyed } from "@/lib/platform/keyed-state";
import type { Keyed } from "@/lib/platform/keyed-state";

/**
 * `useState` that starts over when `resetKey` changes, without remounting.
 * A `key` on the form would do the same but remount it, which drops keyboard
 * focus and the result notice. The rule is in `lib/platform/keyed-state.ts`.
 */
export function useKeyedState<T>(
  initial: T,
  resetKey: string,
): [T, Dispatch<SetStateAction<T>>] {
  const [held, setHeld] = useState<Keyed<T>>({
    key: resetKey,
    value: initial,
  });
  return [
    keyedValue(held, resetKey, initial),
    (next) => {
      setHeld((previous) => writeKeyed(previous, resetKey, initial, next));
    },
  ];
}
