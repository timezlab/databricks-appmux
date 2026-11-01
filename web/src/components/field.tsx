"use client";

import { useId } from "react";
import type { ReactNode } from "react";

import { Label } from "./ui/label";

/**
 * What a control needs to belong to its `Field`: spread it on the `<input>`,
 * `<select>` or `<textarea>`. `id` ties the label to the control;
 * `aria-describedby` makes a screen reader read the hint and the error when the
 * control gets focus; `aria-invalid` marks it (the red border is not alone).
 */
export interface FieldControlProps {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
  "aria-required": true | undefined;
}

export interface FieldProps {
  label: string;
  /** Help shown under the control, always. */
  hint?: string;
  /** The reason the value is not accepted, or `undefined`/`null` when it is. Plain text. */
  error?: string | null;
  /** Adds "(required)" to the label. Validation itself is the form's (`fieldErrorsFromIssues`). */
  isRequired?: boolean;
  /** `inline` puts the control before the label, for a checkbox or radio. */
  layout?: "stacked" | "inline";
  /** Renders the control: `{(control) => <input {...control} name="slug" ... />}`. */
  children: (control: FieldControlProps) => ReactNode;
}

/**
 * The reason a value is not accepted, drawn the one way everywhere: a mark, the
 * word "Error" for a screen reader, then the sentence. `id` is what the
 * control's `aria-describedby` points at.
 */
export function FieldError({
  id,
  children,
}: {
  id: string;
  children: ReactNode;
}) {
  return (
    <p id={id} className="text-sm font-semibold text-destructive">
      <span aria-hidden="true">✕ </span>
      <span className="sr-only">Error: </span>
      {children}
    </p>
  );
}

/**
 * A label, a control, and under the control its hint and its error, tied
 * together so none of them depends on position or colour. The order is the same
 * on every form: the label, then the control, then the words about it, so the
 * controls of fields side by side start at the same height whether or not a
 * field has a hint or an error (a hint above the control would push it down).
 */
export function Field({
  label,
  hint,
  error,
  isRequired = false,
  layout = "stacked",
  children,
}: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const hasError = error !== undefined && error !== null && error !== "";
  const describedBy =
    [hint === undefined ? null : hintId, hasError ? errorId : null]
      .filter((value) => value !== null)
      .join(" ") || undefined;

  const control = children({
    id,
    "aria-describedby": describedBy,
    "aria-invalid": hasError ? true : undefined,
    "aria-required": isRequired ? true : undefined,
  });

  const labelText = (
    <Label htmlFor={id} className="text-base leading-normal font-semibold">
      {label}
      {isRequired ? (
        <span className="font-normal text-muted-foreground"> (required)</span>
      ) : null}
    </Label>
  );
  const notes = (
    <>
      {hint === undefined ? null : (
        <p id={hintId} className="text-sm text-muted-foreground">
          {hint}
        </p>
      )}
      {hasError ? <FieldError id={errorId}>{error}</FieldError> : null}
    </>
  );

  if (layout === "inline") {
    return (
      <div className="flex items-start gap-2">
        {control}
        <div className="flex flex-col gap-1">
          {labelText}
          {notes}
        </div>
      </div>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {labelText}
      {control}
      {notes}
    </div>
  );
}
