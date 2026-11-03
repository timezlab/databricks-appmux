"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { registerApp } from "@/lib/api";
import type { PlatformView } from "@/lib/api";
import { EMPTY_REGISTER_FORM, parseRegisterForm } from "@/lib/platform/apps";
import type { RegisterFormValues } from "@/lib/platform/apps";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import {
  BROWSE_PATH_HINT,
  SourceFolderInput,
} from "@/components/folder-browser";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { Section } from "@/components/section";
import { Input } from "@/components/ui/input";
import { ActionResult } from "./action-result";
import { FormSummary } from "./form-summary";
import { useWriteAction } from "./use-write-action";

/**
 * Registers an app directly, without a request (design 9.3). Gate fields apply
 * to requests, not to an admin's own registration. The page is a platform
 * admin's, whom `GET /workspace/browse` always answers, so Browse is offered.
 */
export function RegisterSection({ view }: { view: PlatformView }) {
  const [values, setValues] = useState<RegisterFormValues>(EMPTY_REGISTER_FORM);
  // Messages stay hidden until the first Register, then follow the typing.
  const [hasTried, setHasTried] = useState(false);
  const action = useWriteAction();

  const parsed = parseRegisterForm(
    values,
    view.apps.map((app) => app.slug),
  );
  const errors = hasTried && !parsed.ok ? parsed.errors : {};

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setHasTried(true);
    if (!parsed.ok) return;
    const body = parsed.value;
    void action.run(async () => {
      const detail = await registerApp(body);
      setValues(EMPTY_REGISTER_FORM);
      setHasTried(false);
      return {
        tone: "success",
        title: `Registered ${detail.slug}`,
        detail: `Owned by ${detail.owner}; it appears in the list of apps.`,
      };
    });
  }

  return (
    <Section title="Register an app">
      <form
        className={`${FORM_STACK_CLASSES} max-w-140`}
        onSubmit={handleSubmit}
      >
        <p className="text-muted-foreground">
          Registers the app now, as you. Someone else asking for an app goes
          through the inbox instead.
        </p>
        <FormSummary errors={errors} />
        <FieldGrid>
          <Field
            label="Slug"
            hint="Lower-case letters, digits and -, at most 31 characters. It is the app's URL prefix."
            isRequired
            error={errors.slug}
          >
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={values.slug}
                onChange={(event) => {
                  setValues({ ...values, slug: event.target.value });
                }}
              />
            )}
          </Field>
          <Field label="Display name" isRequired error={errors.display_name}>
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="off"
                value={values.display_name}
                onChange={(event) => {
                  setValues({ ...values, display_name: event.target.value });
                }}
              />
            )}
          </Field>
          <Field
            label="Source folder"
            hint={BROWSE_PATH_HINT}
            isRequired
            error={errors.source_path}
          >
            {(control) => (
              <SourceFolderInput
                control={control}
                value={values.source_path}
                onValueChange={(value) => {
                  setValues((current) => ({ ...current, source_path: value }));
                }}
                canBrowse
                onUseMemory={(memoryMb) => {
                  setValues((current) => ({
                    ...current,
                    memory_budget_mb: String(memoryMb),
                  }));
                }}
              />
            )}
          </Field>
          <Field
            label="Memory budget (MB)"
            hint="Whole megabytes."
            isRequired
            error={errors.memory_budget_mb}
          >
            {(control) => (
              <Input
                {...control}
                type="text"
                inputMode="numeric"
                autoComplete="off"
                value={values.memory_budget_mb}
                onChange={(event) => {
                  setValues({
                    ...values,
                    memory_budget_mb: event.target.value,
                  });
                }}
              />
            )}
          </Field>
          <Field
            label="Owner"
            hint="The owner's email address, for example user@example.com."
            isRequired
            error={errors.owner}
          >
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={values.owner}
                onChange={(event) => {
                  setValues({ ...values, owner: event.target.value });
                }}
              />
            )}
          </Field>
        </FieldGrid>
        <FormActions
          primary={
            <Button
              type="submit"
              variant="primary"
              isBusy={action.isBusy}
              busyLabel="Registering"
            >
              Register app
            </Button>
          }
        />
        <ActionResult action={action} context="Could not register the app" />
      </form>
    </Section>
  );
}
