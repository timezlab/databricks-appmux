import type { ReactNode } from "react";

/**
 * How every form is laid out, defined once so a page cannot drift from it:
 *
 *  - Fields stack with one gap (`FORM_STACK_CLASSES`, 1rem): label, control,
 *    hint, error (see `Field`). Fields side by side go in a `FieldGrid`, whose
 *    cells are all the same stack, so the controls of one row start at the same
 *    height and have the same height (`h-10`; the small buttons, `h-8`, are for
 *    table rows and dense lists only).
 *  - The buttons of a form go in `FormActions`, the primary one last.
 */

/** The `className` of a `<form>`: its fields and its actions, one gap apart. */
export const FORM_STACK_CLASSES = "flex flex-col gap-4";

/** Fields side by side where there is room, one per row where there is not (a column is at least 18rem). */
export function FieldGrid({ children }: { children: ReactNode }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,18rem),1fr))] gap-4">
      {children}
    </div>
  );
}

export interface FormActionsProps {
  /** The button that goes through with the form. Drawn last, where the eye ends. */
  primary: ReactNode;
  /** Cancel, Discard and the like, drawn before the primary button. */
  children?: ReactNode;
}

/** The row of buttons under a form: the secondary ones first, the primary one last. */
export function FormActions({ primary, children }: FormActionsProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {children}
      {primary}
    </div>
  );
}
