import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

/**
 * Moves the keyboard to the first invalid field each time a send is stopped by
 * the checks, not only the first time. A counter drives the effect because the
 * same errors can appear twice in a row.
 */
export function useFocusFirstInvalid(): {
  formRef: RefObject<HTMLFormElement | null>;
  reportInvalid: () => void;
} {
  const formRef = useRef<HTMLFormElement>(null);
  const [stoppedSends, setStoppedSends] = useState(0);

  useEffect(() => {
    if (stoppedSends === 0) return;
    formRef.current
      ?.querySelector<HTMLElement>('[aria-invalid="true"]')
      ?.focus();
  }, [stoppedSends]);

  return {
    formRef,
    reportInvalid: () => {
      setStoppedSends((count) => count + 1);
    },
  };
}
