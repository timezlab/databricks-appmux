"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { putGrant } from "@/lib/api";
import type { AppDetail, AppPermission, GrantType } from "@/lib/api";
import { withoutError } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { grantActionMessage } from "@/lib/app/outcomes";
import {
  EMPTY_GRANT_FORM,
  buildGrantRequest,
  changeGrantType,
  permissionLabel,
} from "@/lib/app/sharing";
import type { GrantFormValues } from "@/lib/app/sharing";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import { FORM_STACK_CLASSES, FormActions } from "@/components/form-layout";
import { Input } from "@/components/ui/input";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { FormNotices } from "./form-notices";
import type { AppActions } from "./use-app-actions";
import { useFocusFirstInvalid } from "./use-form-focus";

export interface GrantFormProps {
  app: AppDetail;
  actions: AppActions;
  isDisabled: boolean;
  onReload: () => void;
}

const TYPE_LABEL: Record<GrantType, string> = {
  user: "A person",
  group: "A group",
  everyone: "Everyone",
  service_principal: "A service principal",
};

const GRANT_TYPES: readonly GrantType[] = ["user", "group", "everyone"];
const PERMISSIONS: readonly AppPermission[] = ["CAN_USE", "CAN_MANAGE"];

function isGrantType(value: string): value is GrantType {
  return GRANT_TYPES.some((type) => type === value);
}

function isPermission(value: string): value is AppPermission {
  return PERMISSIONS.some((permission) => permission === value);
}

/** Gives a person, a group or everyone access, or changes the level of one who has it (`PUT /grants`). */
export function GrantForm({
  app,
  actions,
  isDisabled,
  onReload,
}: GrantFormProps) {
  const [values, setValues] = useState<GrantFormValues>(EMPTY_GRANT_FORM);
  const [errors, setErrors] = useState<FormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const state = actions.stateOf("grant:add");
  const isEveryone = values.type === "everyone";

  async function send(): Promise<void> {
    const built = buildGrantRequest(app, values);
    if (!built.ok) {
      setErrors(built.errors);
      reportInvalid();
      return;
    }
    setErrors({});
    setRefusal(null);
    const grant = built.body;
    const result = await actions.run(
      "grant:add",
      () => putGrant(app.slug, grant),
      grantActionMessage(
        "add",
        grant.type === "everyone" ? "Everyone" : grant.name,
      ),
    );
    if (result.ok) {
      setValues(changeGrantType(EMPTY_GRANT_FORM, values.type));
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
        aria-label="Share this app"
      >
        <h3>Share this app</h3>
        <FormNotices
          title="The access was not changed"
          errors={errors}
          refusal={refusal}
          onReload={onReload}
        />
        <Field label="Who" error={errors.type}>
          {(control) => (
            <NativeSelect
              {...control}
              name="type"
              value={values.type}
              disabled={isDisabled}
              onChange={(event) => {
                const { value } = event.target;
                if (!isGrantType(value)) return;
                setValues((current) => changeGrantType(current, value));
                setErrors({});
              }}
            >
              {GRANT_TYPES.map((type) => (
                <NativeSelectOption key={type} value={type}>
                  {TYPE_LABEL[type]}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </Field>
        {isEveryone ? (
          <p className="text-muted-foreground">
            Everyone who signs in to the workspace can open the app (Can use).
          </p>
        ) : (
          <>
            <Field
              label={values.type === "user" ? "Email address" : "Group name"}
              hint={
                values.type === "user"
                  ? "Such as user@example.com."
                  : "The name of a Databricks group."
              }
              error={errors.name}
              isRequired
            >
              {(control) => (
                <Input
                  {...control}
                  name="name"
                  type={values.type === "user" ? "email" : "text"}
                  value={values.name}
                  disabled={isDisabled}
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  onChange={(event) => {
                    const { value } = event.target;
                    setValues((current) => ({ ...current, name: value }));
                    setErrors((current) => withoutError(current, "name"));
                  }}
                />
              )}
            </Field>
            <Field
              label="Level"
              hint="Can use opens the app. Can manage also changes it, as an admin of the app."
              error={errors.permission}
            >
              {(control) => (
                <NativeSelect
                  {...control}
                  name="permission"
                  value={values.permission}
                  disabled={isDisabled}
                  onChange={(event) => {
                    const { value } = event.target;
                    if (!isPermission(value)) return;
                    setValues((current) => ({ ...current, permission: value }));
                    setErrors((current) => withoutError(current, "permission"));
                  }}
                >
                  {PERMISSIONS.map((permission) => (
                    <NativeSelectOption key={permission} value={permission}>
                      {permissionLabel(permission)}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              )}
            </Field>
          </>
        )}
        <FormActions
          primary={
            <Button
              type="submit"
              variant="primary"
              isBusy={state === "busy"}
              busyLabel="Sharing"
              disabled={isDisabled || state === "blocked"}
            >
              Share
            </Button>
          }
        />
      </form>
    </Card>
  );
}
