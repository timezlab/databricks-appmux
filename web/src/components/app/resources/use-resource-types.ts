"use client";

import { useCallback, useEffect, useState } from "react";

import { listResourceTypes } from "@/lib/api";
import type { ResourceCatalogue } from "@/lib/app/resources";

export type ResourceTypesState =
  | { status: "loading" }
  | { status: "ready"; catalogue: ResourceCatalogue }
  | { status: "failed"; error: unknown };

/**
 * The catalogue of resource types (`GET /resources/types`), read once when
 * the Resources page opens; it changes only with the server. `reload` asks
 * again after a failure.
 */
export function useResourceTypes(): {
  state: ResourceTypesState;
  reload: () => void;
} {
  const [state, setState] = useState<ResourceTypesState>({
    status: "loading",
  });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    listResourceTypes({ signal: controller.signal }).then(
      (list) => {
        if (!controller.signal.aborted) {
          setState({ status: "ready", catalogue: list.items });
        }
      },
      (error: unknown) => {
        // A call aborted because the page left is nobody's failure.
        if (!controller.signal.aborted) setState({ status: "failed", error });
      },
    );
    return () => {
      controller.abort();
    };
  }, [attempt]);

  const reload = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((current) => current + 1);
  }, []);

  return { state, reload };
}
