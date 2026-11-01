import { useReducer } from "react";

import { getReleaseSourceFile, listReleaseSource } from "@/lib/api";
import { CLOSED_SOURCE_VIEW, sourceViewReducer } from "@/lib/app/source";
import type { SourceViewState } from "@/lib/app/source";

export interface SourceViewer {
  state: SourceViewState;
  open: (releaseNo: number) => void;
  close: () => void;
  openFile: (releaseNo: number, path: string) => void;
  closeFile: () => void;
}

/**
 * The source viewer's state and the two reads behind it. Nothing is fetched
 * until a person presses a button: each list and each file writes an audit row
 * on the server, so the viewer must never run on a timer or on mount. A release
 * that disappears from `releaseNos` closes the viewer.
 */
export function useSourceViewer(
  slug: string,
  releaseNos: readonly number[],
): SourceViewer {
  const [held, dispatch] = useReducer(sourceViewReducer, CLOSED_SOURCE_VIEW);
  // Pure, so it runs while rendering instead of in an effect.
  const state = sourceViewReducer(held, {
    type: "releases-changed",
    releaseNos,
  });

  async function loadListing(releaseNo: number): Promise<void> {
    try {
      const listing = await listReleaseSource(slug, releaseNo);
      dispatch({ type: "listing-loaded", releaseNo, listing });
    } catch (error) {
      // Kept in the state and shown by the viewer, next to the list it replaces.
      dispatch({ type: "listing-failed", releaseNo, error });
    }
  }

  async function loadFile(releaseNo: number, path: string): Promise<void> {
    try {
      const file = await getReleaseSourceFile(slug, releaseNo, path);
      dispatch({ type: "file-loaded", releaseNo, path, file });
    } catch (error) {
      dispatch({ type: "file-failed", releaseNo, path, error });
    }
  }

  return {
    state,
    open: (releaseNo) => {
      dispatch({ type: "open", releaseNo });
      void loadListing(releaseNo);
    },
    close: () => {
      dispatch({ type: "close" });
    },
    openFile: (releaseNo, path) => {
      dispatch({ type: "file-requested", releaseNo, path });
      void loadFile(releaseNo, path);
    },
    closeFile: () => {
      dispatch({ type: "file-closed" });
    },
  };
}
