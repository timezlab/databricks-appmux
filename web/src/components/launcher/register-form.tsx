"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { createRequest } from "@/lib/api";
import type { RequestRow } from "@/lib/api";
import {
  EMPTY_REGISTER_FORM,
  REASON_MAX_LENGTH,
  submitRegisterForm,
  withoutFieldError,
} from "@/lib/launcher/register-form";
import type {
  RegisterFormContext,
  RegisterFormErrors,
  RegisterFormField,
  RegisterFormValues,
} from "@/lib/launcher/register-form";
import { Button } from "@/components/button";
import { ErrorNotice } from "@/components/error-notice";
import { FORM_ERROR_KEY } from "@/lib/forms/field-errors";
import { Field } from "@/components/field";
import {
  BROWSE_PATH_HINT,
  SourceFolderInput,
  TYPE_PATH_HINT,
} from "@/components/folder-browser";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { Notice } from "@/components/notice";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export interface RegisterFormProps {
  /** What the page already knows, for the courtesy checks before sending. */
  context: RegisterFormContext;
  /** The server accepted the request; the form is done. */
  onSent: (request: RequestRow) => void;
  onCancel: () => void;
  /** Fetches the page's data again: offered when the server answers 409. */
  onReload: () => void;
  /**
   * Offer the folder browser beside the source folder (`mayBrowseWorkspace`):
   * the route answers 403 to a plain visitor, who types the path. Default
   * `false`, so nothing leads to a refusal while the answer is unknown.
   */
  canBrowseWorkspace?: boolean;
}

/**
 * "Request a new app" (design 9.4). The rules are checked here first (they are
 * the contract's, see `lib/launcher/register-form.ts`); whatever the server then
 * refuses is shown as it is, with a reload button after a 409.
 */
export function RegisterForm({
  context,
  onSent,
  onCancel,
  onReload,
  canBrowseWorkspace = false,
}: RegisterFormProps) {
  const [values, setValues] = useState<RegisterFormValues>(EMPTY_REGISTER_FORM);
  const [errors, setErrors] = useState<RegisterFormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const [isSending, setIsSending] = useState(false);
  // Counts the sends the checks stopped, so the focus moves to the first
  // problem each time and not only the first time.
  const [stoppedSends, setStoppedSends] = useState(0);

  const formRef = useRef<HTMLFormElement>(null);
  const slugRef = useRef<HTMLInputElement>(null);

  // The form opens from a button press: the keyboard goes to its first field.
  useEffect(() => {
    slugRef.current?.focus();
  }, []);

  useEffect(() => {
    if (stoppedSends === 0) return;
    formRef.current
      ?.querySelector<HTMLElement>('[aria-invalid="true"]')
      ?.focus();
  }, [stoppedSends]);

  function setField(field: RegisterFormField, value: string): void {
    setValues((current) => ({ ...current, [field]: value }));
    setErrors((current) => withoutFieldError(current, field));
  }

  async function send(): Promise<void> {
    if (isSending) return;
    setIsSending(true);
    setRefusal(null);
    const result = await submitRegisterForm(values, context, createRequest);
    setIsSending(false);
    switch (result.status) {
      case "invalid":
        setErrors(result.errors);
        setStoppedSends((count) => count + 1);
        return;
      case "refused":
        setRefusal({ error: result.error });
        return;
      case "sent":
        onSent(result.request);
        return;
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    void send();
  }

  const formError = errors[FORM_ERROR_KEY];
  const hasErrors = Object.keys(errors).length > 0;

  return (
    <form
      ref={formRef}
      onSubmit={handleSubmit}
      noValidate
      className={FORM_STACK_CLASSES}
    >
      <p className="text-muted-foreground">
        A platform admin reviews the request. The app is registered only if they
        approve it, and you can withdraw the request until then.
      </p>

      {hasErrors ? (
        <Notice tone="danger" title="The request was not sent" live="assertive">
          <p>
            {formError ?? "Fix the problems marked below, then send it again."}
          </p>
        </Notice>
      ) : null}
      {refusal === null ? null : (
        <ErrorNotice
          error={refusal.error}
          context="The request was not sent"
          onReload={onReload}
        />
      )}

      <FieldGrid>
        <Field
          label="Slug"
          hint="The app opens at /slug/. 1 to 31 lower-case letters, digits or hyphens."
          error={errors.slug}
          isRequired
        >
          {(control) => (
            <Input
              {...control}
              ref={slugRef}
              name="slug"
              type="text"
              value={values.slug}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              onChange={(event) => {
                setField("slug", event.target.value);
              }}
            />
          )}
        </Field>

        <Field
          label="Name"
          hint="What people see on the launcher."
          error={errors.displayName}
          isRequired
        >
          {(control) => (
            <Input
              {...control}
              name="display_name"
              type="text"
              value={values.displayName}
              autoComplete="off"
              onChange={(event) => {
                setField("displayName", event.target.value);
              }}
            />
          )}
        </Field>

        <Field
          label="Source folder"
          hint={canBrowseWorkspace ? BROWSE_PATH_HINT : TYPE_PATH_HINT}
          error={errors.sourcePath}
          isRequired
        >
          {(control) => (
            <SourceFolderInput
              control={control}
              name="source_path"
              value={values.sourcePath}
              onValueChange={(value) => {
                setField("sourcePath", value);
              }}
              canBrowse={canBrowseWorkspace}
            />
          )}
        </Field>
      </FieldGrid>

      <p className="text-muted-foreground">
        The platform admin sets the memory budget of a new app. Once the app
        exists, its admin can ask for more in Settings.
      </p>

      <Field
        label="Reason"
        hint={`Why you need this app, so the platform admin can decide. ${values.reason.length} of ${REASON_MAX_LENGTH} characters.`}
        error={errors.reason}
        isRequired
      >
        {(control) => (
          <Textarea
            {...control}
            name="reason"
            value={values.reason}
            onChange={(event) => {
              setField("reason", event.target.value);
            }}
          />
        )}
      </Field>

      <FormActions
        primary={
          <Button
            type="submit"
            variant="primary"
            isBusy={isSending}
            busyLabel="Sending"
          >
            Send request
          </Button>
        }
      >
        <Button onClick={onCancel}>Cancel</Button>
      </FormActions>
    </form>
  );
}
