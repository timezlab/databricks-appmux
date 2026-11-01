"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { updateRelease } from "@/lib/api";
import type { AppDetail, ReleaseSummary } from "@/lib/api";
import { withoutError } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { releaseActionMessage } from "@/lib/app/outcomes";
import {
  RELEASE_NAME_MAX_LENGTH,
  RELEASE_TAGS_MAX,
  RELEASE_TAG_MAX_LENGTH,
  buildReleaseUpdate,
  isReleaseEditChanged,
  releaseEditDefaults,
} from "@/lib/app/release-edit";
import type { ReleaseEditValues } from "@/lib/app/release-edit";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import { FORM_STACK_CLASSES, FormActions } from "@/components/form-layout";
import { Input } from "@/components/ui/input";
import { FormNotices } from "./form-notices";
import type { AppActions } from "./use-app-actions";
import { useFocusFirstInvalid } from "./use-form-focus";

export interface ReleaseEditFormProps {
  app: AppDetail;
  release: ReleaseSummary;
  actions: AppActions;
  onClose: () => void;
  onReload: () => void;
}

/** Name and tags of one release. The server replaces both values, so both are always sent. */
export function ReleaseEditForm({
  app,
  release,
  actions,
  onClose,
  onReload,
}: ReleaseEditFormProps) {
  const releaseNo = release.release_no;
  const [values, setValues] = useState<ReleaseEditValues>(() =>
    releaseEditDefaults(release),
  );
  const [errors, setErrors] = useState<FormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const nameRef = useRef<HTMLInputElement>(null);

  // The form opens from a button press: the keyboard goes to its first field.
  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const state = actions.stateOf(`edit:${releaseNo}`);

  function setField(field: keyof ReleaseEditValues, value: string): void {
    setValues((current) => ({ ...current, [field]: value }));
    setErrors((current) => withoutError(current, field));
  }

  async function send(): Promise<void> {
    const built = buildReleaseUpdate(values);
    if (!built.ok) {
      setErrors(built.errors);
      reportInvalid();
      return;
    }
    setErrors({});
    setRefusal(null);
    const result = await actions.run(
      `edit:${releaseNo}`,
      () => updateRelease(app.slug, releaseNo, built.body),
      releaseActionMessage("edit", releaseNo),
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
        aria-label={`Edit release ${releaseNo}`}
      >
        <h3 className="wrap-break-word">Edit release {releaseNo}</h3>
        <FormNotices
          title="The release was not saved"
          errors={errors}
          refusal={refusal}
          onReload={onReload}
        />
        <Field
          label="Name"
          hint={`At most ${RELEASE_NAME_MAX_LENGTH} characters. Leave it empty for no name.`}
          error={errors.name}
        >
          {(control) => (
            <Input
              {...control}
              ref={nameRef}
              name="name"
              type="text"
              value={values.name}
              autoComplete="off"
              onChange={(event) => {
                setField("name", event.target.value);
              }}
            />
          )}
        </Field>
        <Field
          label="Tags"
          hint={`Separated by commas or spaces, in lower case: up to ${RELEASE_TAGS_MAX} tags of ${RELEASE_TAG_MAX_LENGTH} characters.`}
          error={errors.tags}
        >
          {(control) => (
            <Input
              {...control}
              name="tags"
              type="text"
              value={values.tags}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              onChange={(event) => {
                setField("tags", event.target.value);
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
              busyLabel="Saving"
              disabled={
                state === "blocked" || !isReleaseEditChanged(values, release)
              }
            >
              Save
            </Button>
          }
        >
          <Button onClick={onClose}>Cancel</Button>
        </FormActions>
      </form>
    </Card>
  );
}
