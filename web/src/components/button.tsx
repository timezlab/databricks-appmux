"use client";

import { LoaderCircleIcon } from "lucide-react";
import type { ComponentPropsWithRef, MouseEvent } from "react";

import { Button as UiButton } from "./ui/button";

/**
 * `danger` is the outlined look of a destructive control at rest; `danger-filled`
 * is for the confirm step that goes through with it. Danger is told apart by its
 * outline, its words and the confirm step, never by its red alone.
 *
 * Two rules for every row of buttons: the primary one comes last (Cancel or
 * Discard, then Save), and a button is `normal` (the height of a field control,
 * h-10) except inside a table row, a dense list or a notice, where it is `small`
 * (h-8).
 */
export type ButtonVariant =
  "secondary" | "primary" | "danger" | "danger-filled" | "ghost";
export type ButtonSize = "normal" | "small";

const UI_VARIANTS = {
  secondary: "outline",
  primary: "default",
  danger: "destructive-outline",
  "danger-filled": "destructive",
  ghost: "ghost",
} as const satisfies Record<ButtonVariant, string>;

const UI_SIZES = {
  normal: "default",
  small: "sm",
} as const satisfies Record<ButtonSize, string>;

/**
 * The shadcn `variant` and `size` that draw a kit variant and size. Shared with
 * `AppLink`, which draws a link as a button.
 */
export function uiButtonLook(variant: ButtonVariant, size: ButtonSize) {
  return { variant: UI_VARIANTS[variant], size: UI_SIZES[size] };
}

// No className, style or raw HTML: looks come from `variant` and `size`, so a
// page cannot step outside the strict CSP (no inline styles) by accident.
export type ButtonProps = Omit<
  ComponentPropsWithRef<"button">,
  "className" | "style" | "dangerouslySetInnerHTML"
> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /**
   * A call is running. The button stays focusable (a disabled one drops focus)
   * but ignores clicks and shows a spinner, so one action cannot be sent twice.
   * Say it in words too: a button's busy state is not announced, so show the
   * result in a `Notice` or `LiveRegion`.
   */
  isBusy?: boolean;
  /** Replaces the label while busy ("Saving"). */
  busyLabel?: string;
};

/** A `<button type="button">` unless `type` says otherwise, so it never submits a form by accident. */
export function Button({
  variant = "secondary",
  size = "normal",
  isBusy = false,
  busyLabel,
  type = "button",
  onClick,
  children,
  ...rest
}: ButtonProps) {
  function handleClick(event: MouseEvent<HTMLButtonElement>): void {
    if (isBusy) {
      // Also stops a busy submit button from submitting the form again.
      event.preventDefault();
      return;
    }
    onClick?.(event);
  }

  return (
    <UiButton
      {...rest}
      {...uiButtonLook(variant, size)}
      type={type}
      className="aria-busy:cursor-progress"
      aria-busy={isBusy || undefined}
      aria-disabled={isBusy || undefined}
      onClick={handleClick}
    >
      {isBusy ? (
        <LoaderCircleIcon className="animate-spinner" aria-hidden="true" />
      ) : null}
      {isBusy && busyLabel !== undefined ? busyLabel : children}
    </UiButton>
  );
}
