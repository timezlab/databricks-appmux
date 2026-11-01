import { FORM_ERROR_KEY } from "@/lib/platform/forms";
import type { FormErrors } from "@/lib/platform/forms";
import { Notice } from "@/components/notice";

/**
 * The one line above a form that failed its check. The text is fixed while only
 * fields are wrong, so it does not announce again on every keystroke of the
 * live validation; each field names its own problem.
 */
export function FormSummary({ errors }: { errors: FormErrors }) {
  const formMessage = errors[FORM_ERROR_KEY];
  const hasFieldErrors = Object.keys(errors).some(
    (key) => key !== FORM_ERROR_KEY,
  );
  if (formMessage === undefined && !hasFieldErrors) return null;
  return (
    <Notice
      tone="danger"
      live="assertive"
      title={formMessage ?? "Some fields need attention"}
    />
  );
}
