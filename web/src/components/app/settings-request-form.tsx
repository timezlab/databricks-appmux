"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { createRequest } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import { withoutError } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { requestSentMessage } from "@/lib/app/outcomes";
import {
  MEMORY_BUDGET_MAX_MB,
  MEMORY_BUDGET_MIN_MB,
  REQUEST_REASON_MAX_LENGTH,
  buildSettingsRequest,
  settingsFormDefaults,
} from "@/lib/app/requests";
import type { SettingsFormField, SettingsFormValues } from "@/lib/app/requests";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import { FORM_STACK_CLASSES, FormActions } from "@/components/form-layout";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FormNotices } from "./form-notices";
import type { AppActions } from "./use-app-actions";
import { useFocusFirstInvalid } from "./use-form-focus";

export interface SettingsRequestFormProps {
  app: AppDetail;
  actions: AppActions;
  onClose: () => void;
  onReload: () => void;
}

/**
 * Asks a platform admin to change the app's name, slug, source folder or
 * memory budget. Only the fields that differ from the app's values are sent.
 */
export function SettingsRequestForm({
  app,
  actions,
  onClose,
  onReload,
}: SettingsRequestFormProps) {
  const [values, setValues] = useState<SettingsFormValues>(() =>
    settingsFormDefaults(app),
  );
  const [errors, setErrors] = useState<FormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const firstFieldRef = useRef<HTMLInputElement>(null);
  const state = actions.stateOf("request:settings");

  // The form opens from a button press: the keyboard goes to its first field.
  useEffect(() => {
    firstFieldRef.current?.focus();
  }, []);

  function setField(field: SettingsFormField, value: string): void {
    setValues((current) => ({ ...current, [field]: value }));
    setErrors((current) => withoutError(current, field));
  }

  async function send(): Promise<void> {
    const built = buildSettingsRequest(app, values);
    if (!built.ok) {
      setErrors(built.errors);
      reportInvalid();
      return;
    }
    setErrors({});
    setRefusal(null);
    const result = await actions.run(
      "request:settings",
      () => createRequest(built.body),
      requestSentMessage("settings"),
    );
    if (result.ok) {
      onClose();
    } else {
      setRefusal({ error: result.error });
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    void send();
  }

  return (
    <Card asChild>
      <form
        ref={formRef}
        onSubmit={handleSubmit}
        noValidate
        className={FORM_STACK_CLASSES}
        aria-label="Request a change of settings"
      >
        <h3>Request a change of settings</h3>
        <p className="text-muted-foreground">
          A platform admin reviews the request. Change the fields you want
          changed; the others are left as they are. You can withdraw the request
          until it is answered.
        </p>
        <FormNotices
          title="The request was not sent"
          errors={errors}
          refusal={refusal}
          onReload={onReload}
        />
        <Field
          label="Slug"
          hint="The app opens at /slug/. A new slug moves its address. 1 to 31 lower-case letters, digits or hyphens."
          error={errors.slug}
        >
          {(control) => (
            <Input
              {...control}
              ref={firstFieldRef}
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
          hint="The workspace folder the app is read from. A new folder is read at the next Update."
          error={errors.sourcePath}
        >
          {(control) => (
            <Input
              {...control}
              name="source_path"
              type="text"
              value={values.sourcePath}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              onChange={(event) => {
                setField("sourcePath", event.target.value);
              }}
            />
          )}
        </Field>
        <Field
          label="Memory budget (MB)"
          hint={`A whole number from ${MEMORY_BUDGET_MIN_MB} to ${MEMORY_BUDGET_MAX_MB}.`}
          error={errors.memoryBudgetMb}
        >
          {(control) => (
            <Input
              {...control}
              name="memory_budget_mb"
              type="text"
              inputMode="numeric"
              value={values.memoryBudgetMb}
              autoComplete="off"
              onChange={(event) => {
                setField("memoryBudgetMb", event.target.value);
              }}
            />
          )}
        </Field>
        <Field
          label="Reason"
          hint={`Why you ask, so the platform admin can decide. ${values.reason.length} of ${REQUEST_REASON_MAX_LENGTH} characters.`}
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
              isBusy={state === "busy"}
              busyLabel="Sending"
              disabled={state === "blocked"}
            >
              Send request
            </Button>
          }
        >
          <Button onClick={onClose}>Cancel</Button>
        </FormActions>
      </form>
    </Card>
  );
}
