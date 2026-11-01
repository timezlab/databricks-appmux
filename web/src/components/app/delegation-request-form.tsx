"use client";

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { createRequest } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import { assertNever } from "@/lib/assert-never";
import { withoutError } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { requestSentMessage } from "@/lib/app/outcomes";
import {
  EMPTY_DELEGATION_FORM,
  REQUEST_REASON_MAX_LENGTH,
  buildDelegationRequest,
  delegationChoices,
} from "@/lib/app/requests";
import type {
  DelegationChoice,
  DelegationFormValues,
} from "@/lib/app/requests";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import { FORM_STACK_CLASSES, FormActions } from "@/components/form-layout";
import { NativeCheckbox } from "@/components/ui/native-choice";
import { Textarea } from "@/components/ui/textarea";
import { FormNotices } from "./form-notices";
import type { AppActions } from "./use-app-actions";
import { useFocusFirstInvalid } from "./use-form-focus";

export interface DelegationRequestFormProps {
  app: AppDetail;
  actions: AppActions;
  onClose: () => void;
  onReload: () => void;
}

function choiceHint(choice: DelegationChoice, askedFor: string): string {
  switch (choice.state) {
    case "available":
      return askedFor;
    case "delegated":
      return `Already delegated to this app. ${askedFor}`;
    case "not_allowed":
      return `A platform admin has switched this delegation off, so a request for it is refused. ${askedFor}`;
    default:
      return assertNever(choice);
  }
}

/** Asks a platform admin to let the app act on behalf of its visitors (OBO) or as the shared service principal. */
export function DelegationRequestForm({
  app,
  actions,
  onClose,
  onReload,
}: DelegationRequestFormProps) {
  const [values, setValues] = useState<DelegationFormValues>(
    EMPTY_DELEGATION_FORM,
  );
  const [errors, setErrors] = useState<FormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const choices = delegationChoices(app);
  const state = actions.stateOf("request:delegation");

  // The form opens from a button press: the keyboard goes to its first field.
  // The first choice may be switched off, so the first control that is on gets it.
  useEffect(() => {
    formRef.current
      ?.querySelector<HTMLElement>("input:not(:disabled), textarea")
      ?.focus();
  }, [formRef]);

  async function send(): Promise<void> {
    const built = buildDelegationRequest(app, values);
    if (!built.ok) {
      setErrors(built.errors);
      reportInvalid();
      return;
    }
    setErrors({});
    setRefusal(null);
    const result = await actions.run(
      "request:delegation",
      () => createRequest(built.body),
      requestSentMessage("delegation"),
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
        aria-label="Request a delegation"
      >
        <h3>Request a delegation</h3>
        <p className="text-muted-foreground">
          A delegation lets the app reach Databricks with someone&apos;s rights.
          A platform admin decides; you can withdraw the request until then.
        </p>
        <FormNotices
          title="The request was not sent"
          errors={errors}
          refusal={refusal}
          onReload={onReload}
        />
        <Field
          layout="inline"
          label="Act on behalf of the people who open the app (OBO)"
          hint={choiceHint(
            choices.obo,
            "The app uses each visitor's own rights.",
          )}
          error={errors.obo}
        >
          {(control) => (
            <NativeCheckbox
              {...control}
              name="obo"
              checked={values.obo}
              disabled={choices.obo.state !== "available"}
              onChange={(event) => {
                const { checked } = event.target;
                setValues((current) => ({ ...current, obo: checked }));
                setErrors((current) => withoutError(current, "obo"));
              }}
            />
          )}
        </Field>
        <Field
          layout="inline"
          label="Act as the appmux service principal"
          hint={choiceHint(
            choices.sharedSp,
            "The app uses the identity appmux itself runs as.",
          )}
          error={errors.sharedSp}
        >
          {(control) => (
            <NativeCheckbox
              {...control}
              name="shared_sp"
              checked={values.sharedSp}
              disabled={choices.sharedSp.state !== "available"}
              onChange={(event) => {
                const { checked } = event.target;
                setValues((current) => ({ ...current, sharedSp: checked }));
                setErrors((current) => withoutError(current, "sharedSp"));
              }}
            />
          )}
        </Field>
        <Field
          label="Reason"
          hint={`Why the app needs it, so the platform admin can decide. ${values.reason.length} of ${REQUEST_REASON_MAX_LENGTH} characters.`}
          error={errors.reason}
          isRequired
        >
          {(control) => (
            <Textarea
              {...control}
              name="reason"
              value={values.reason}
              onChange={(event) => {
                const { value } = event.target;
                setValues((current) => ({ ...current, reason: value }));
                setErrors((current) => withoutError(current, "reason"));
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
