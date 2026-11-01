import { FORM_ERROR_KEY } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { ErrorNotice } from "@/components/error-notice";
import { Notice } from "@/components/notice";

export interface FormNoticesProps {
  /** "The request was not sent". */
  title: string;
  /** What the checks before sending found. */
  errors: FormErrors;
  /** What the server refused, as thrown; `null` when it did not. */
  refusal: { error: unknown } | null;
  /** Reads the page's data again, offered after a 409. */
  onReload: () => void;
}

/** The two things that can stop a form as a whole: our checks, then the server's answer. */
export function FormNotices({
  title,
  errors,
  refusal,
  onReload,
}: FormNoticesProps) {
  const hasErrors = Object.keys(errors).length > 0;
  return (
    <>
      {hasErrors ? (
        <Notice tone="danger" title={title} live="assertive">
          <p>
            {errors[FORM_ERROR_KEY] ??
              "Fix the problems marked below, then send it again."}
          </p>
        </Notice>
      ) : null}
      {refusal === null ? null : (
        <ErrorNotice
          error={refusal.error}
          context={title}
          onReload={onReload}
        />
      )}
    </>
  );
}
