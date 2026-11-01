"use client";

import { TriangleAlertIcon } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { Button } from "./button";
import type { ButtonSize, ButtonVariant } from "./button";

export interface ConfirmButtonProps {
  /** The button shown at rest ("Delete app"). */
  label: string;
  /** The question shown in its place ("Delete sales? Its releases are removed."). */
  prompt: string;
  /** The button that goes ahead; name the action, not "OK" ("Yes, delete sales"). */
  confirmLabel: string;
  /** Default "Cancel". */
  cancelLabel?: string;
  /** Called once, after the person pressed the confirm button. */
  onConfirm: () => void;
  /** Look of the resting button. Default "secondary". */
  variant?: ButtonVariant;
  /** Look of the confirm button. Default "danger-filled". */
  confirmVariant?: ButtonVariant;
  size?: ButtonSize;
  /** The action runs; the resting button shows a spinner (see `Button`). */
  isBusy?: boolean;
  busyLabel?: string;
  isDisabled?: boolean;
}

/**
 * Two steps instead of `window.confirm` (a dialog the CSP-strict, keyboard-first
 * UI avoids): the first press swaps the button for a question with a confirm and
 * a cancel button. Focus lands on Cancel, the safe answer; Escape or Cancel
 * puts it back on the resting button. The prompt is announced as the name of
 * the group. The answer that goes ahead comes last, like the primary button of
 * every other group of buttons. The question sits in a frame, with a warning mark when the
 * confirm button is the danger one, so a destructive step is told apart from
 * the red primary button by its shape and its words, not by its hue.
 */
export function ConfirmButton({
  label,
  prompt,
  confirmLabel,
  cancelLabel = "Cancel",
  onConfirm,
  variant = "secondary",
  confirmVariant = "danger-filled",
  size = "normal",
  isBusy = false,
  busyLabel,
  isDisabled = false,
}: ConfirmButtonProps) {
  const [isAsked, setIsAsked] = useState(false);
  const promptId = useId();
  const restingButtonRef = useRef<HTMLButtonElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const wasAskingRef = useRef(false);

  // A busy or disabled button cannot be confirmed, so the question goes away.
  const isAsking = isAsked && !isBusy && !isDisabled;

  useEffect(() => {
    if (isAsking) {
      cancelButtonRef.current?.focus();
    } else if (wasAskingRef.current) {
      restingButtonRef.current?.focus();
    }
    wasAskingRef.current = isAsking;
  }, [isAsking]);

  function handleKeyDown(event: KeyboardEvent<HTMLElement>): void {
    if (event.key === "Escape") setIsAsked(false);
  }

  if (!isAsking) {
    return (
      <Button
        ref={restingButtonRef}
        variant={variant}
        size={size}
        isBusy={isBusy}
        busyLabel={busyLabel}
        disabled={isDisabled}
        onClick={() => {
          setIsAsked(true);
        }}
      >
        {label}
      </Button>
    );
  }

  const isDanger =
    confirmVariant === "danger-filled" || confirmVariant === "danger";
  return (
    <span
      className={
        isDanger
          ? "inline-flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border-2 border-tone-danger-border bg-tone-danger-bg px-3 py-2 text-tone-danger-text"
          : "inline-flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border-2 border-input bg-card px-3 py-2"
      }
      role="group"
      aria-labelledby={promptId}
      onKeyDown={handleKeyDown}
    >
      {isDanger ? (
        <TriangleAlertIcon className="size-5 shrink-0" aria-hidden="true" />
      ) : null}
      <span id={promptId} className="font-semibold">
        {prompt}
      </span>
      <Button
        ref={cancelButtonRef}
        size={size}
        onClick={() => {
          setIsAsked(false);
        }}
      >
        {cancelLabel}
      </Button>
      <Button
        variant={confirmVariant}
        size={size}
        onClick={() => {
          setIsAsked(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </Button>
    </span>
  );
}
