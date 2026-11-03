"use client";

import { TriangleAlertIcon } from "lucide-react";
import { useEffect, useId, useRef } from "react";

import { Button } from "@/components/button";

export interface ReleaseDeleteConfirmProps {
  releaseNo: number;
  onConfirm: () => void;
  /** Cancel or Escape: nothing is deleted. */
  onCancel: () => void;
}

/**
 * The question a Delete from a row menu asks, under the table: a menu item
 * cannot turn into a question itself, and a modal dialog is what this UI
 * avoids. It looks like `ConfirmButton`'s question (a frame, a warning mark,
 * Cancel first and focused, the action named on the button that goes ahead).
 */
export function ReleaseDeleteConfirm({
  releaseNo,
  onConfirm,
  onCancel,
}: ReleaseDeleteConfirmProps) {
  const promptId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  return (
    <div
      role="group"
      aria-labelledby={promptId}
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border-2 border-tone-danger-border bg-tone-danger-bg px-3 py-2 text-tone-danger-text"
      onKeyDown={(event) => {
        if (event.key === "Escape") onCancel();
      }}
    >
      <TriangleAlertIcon className="size-5 shrink-0" aria-hidden="true" />
      <span id={promptId} className="font-semibold">
        Delete release {releaseNo}? Its snapshot is removed and it cannot be
        activated again.
      </span>
      <Button ref={cancelRef} onClick={onCancel}>
        Cancel
      </Button>
      <Button variant="danger-filled" onClick={onConfirm}>
        Yes, delete release {releaseNo}
      </Button>
    </div>
  );
}
