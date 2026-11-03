"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { putGrant } from "@/lib/api";
import type { AppDetail, AppPermission } from "@/lib/api";
import { withoutError } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { grantActionMessage } from "@/lib/app/outcomes";
import {
  EMPTY_GRANT_FORM,
  buildGrantRequest,
  choosePrincipal,
  grantLevelChoices,
} from "@/lib/app/sharing";
import type { PrincipalChoice } from "@/lib/app/sharing";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { PrincipalCombobox } from "@/components/principal-combobox";
import { Card } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FormNotices } from "./form-notices";
import type { AppActions } from "./use-app-actions";
import { useFocusFirstInvalid } from "./use-form-focus";

export interface GrantFormProps {
  app: AppDetail;
  actions: AppActions;
  onReload: () => void;
}

const LEVEL_HINT =
  "Can use opens the app. Can manage also changes it, as an admin of the app.";

function isPermission(value: string): value is AppPermission {
  return value === "CAN_USE" || value === "CAN_MANAGE";
}

/**
 * The Add row of Permissions: one combobox for a user, group, service
 * principal or everyone, and a level (`PUT /grants`, which also changes the
 * level of a principal that already has one). Can manage is disabled, with
 * the reason under the select, for a principal that cannot hold it.
 */
export function GrantForm({ app, actions, onReload }: GrantFormProps) {
  const [principal, setPrincipal] = useState<PrincipalChoice | null>(null);
  const [permission, setPermission] = useState<AppPermission>(
    EMPTY_GRANT_FORM.permission,
  );
  const [errors, setErrors] = useState<FormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const state = actions.stateOf("grant:add");
  const levels = grantLevelChoices(principal?.type ?? null);
  const blockedLevel = levels.find((level) => !level.isAvailable);

  function changePrincipal(next: PrincipalChoice | null): void {
    const values = choosePrincipal({ principal, permission }, next);
    setPrincipal(next);
    setPermission(values.permission);
    setErrors((current) => withoutError(current, "principal"));
  }

  async function send(): Promise<void> {
    const built = buildGrantRequest(app, { principal, permission });
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
        grant.type === "everyone"
          ? "Everyone"
          : (principal?.label ?? grant.name),
      ),
    );
    if (result.ok) {
      setPrincipal(null);
      setPermission(EMPTY_GRANT_FORM.permission);
      // Ready for the next one: the keyboard goes back to the principal field.
      formRef.current
        ?.querySelector<HTMLElement>('button[role="combobox"]')
        ?.focus();
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
        aria-label="Add a permission"
      >
        <h3>Add a permission</h3>
        <FormNotices
          title="The access was not changed"
          errors={errors}
          refusal={refusal}
          onReload={onReload}
        />
        <FieldGrid>
          <Field
            label="Who"
            hint="A user, a group, a service principal or everyone. Type to search, or type it in full."
            error={errors.principal}
            isRequired
          >
            {(control) => (
              <PrincipalCombobox
                {...control}
                value={principal}
                onValueChange={changePrincipal}
              />
            )}
          </Field>
          <Field
            label="Level"
            hint={
              blockedLevel === undefined
                ? LEVEL_HINT
                : `Can manage: ${blockedLevel.reason ?? ""}`
            }
            error={errors.permission}
          >
            {(control) => (
              <Select
                value={permission}
                onValueChange={(value) => {
                  if (!isPermission(value)) return;
                  setPermission(value);
                  setErrors((current) => withoutError(current, "permission"));
                }}
              >
                <SelectTrigger {...control} className="w-full max-w-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent label="Level">
                  {levels.map((level) => (
                    <SelectItem
                      key={level.value}
                      value={level.value}
                      textValue={level.label}
                      disabled={!level.isAvailable}
                    >
                      <span className="flex flex-col">
                        <span>{level.label}</span>
                        {level.reason === null ? null : (
                          <span className="text-xs text-muted-foreground">
                            {level.reason}
                          </span>
                        )}
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>
        </FieldGrid>
        <FormActions
          primary={
            <Button
              type="submit"
              variant="primary"
              isBusy={state === "busy"}
              busyLabel="Adding"
              disabled={state === "blocked"}
            >
              Add
            </Button>
          }
        />
      </form>
    </Card>
  );
}
