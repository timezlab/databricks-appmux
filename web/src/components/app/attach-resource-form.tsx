"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { attachResource } from "@/lib/api";
import type { AccessAs, AppDetail } from "@/lib/api";
import { withoutError } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { resourceActionMessage } from "@/lib/app/outcomes";
import {
  RESOURCE_TYPES,
  accessAsChoices,
  buildAttachRequest,
  changeDraftType,
  resourceTypeInfo,
} from "@/lib/app/resources";
import type { AttachDraft } from "@/lib/app/resources";
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

export interface AttachResourceFormProps {
  app: AppDetail;
  /** What the inputs hold when the form opens: empty, or an offer filled in. */
  initialDraft: AttachDraft;
  actions: AppActions;
  onClose: () => void;
  onReload: () => void;
}

type AttachTextField = "key" | "target" | "permission";

function isAccessAs(value: string): value is AccessAs {
  return value === "obo" || value === "own_sp" || value === "shared_sp";
}

/**
 * Attach one resource. The attach check (does the attacher's own token reach
 * the target) runs on the server and its refusal is shown as it is; a secret
 * is a `scope/key` reference and its value is never asked for.
 */
export function AttachResourceForm({
  app,
  initialDraft,
  actions,
  onClose,
  onReload,
}: AttachResourceFormProps) {
  const [draft, setDraft] = useState<AttachDraft>(initialDraft);
  const [errors, setErrors] = useState<FormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const firstFieldRef = useRef<HTMLInputElement>(null);

  // The form opens from a button press: the keyboard goes to its first field.
  useEffect(() => {
    firstFieldRef.current?.focus();
  }, []);

  const info = resourceTypeInfo(draft.type);
  const choices = accessAsChoices(app);
  const unavailableChoices = choices.filter((choice) => !choice.isAvailable);
  const state = actions.stateOf("attach");

  function setField(field: AttachTextField, value: string): void {
    setDraft((current) => ({ ...current, [field]: value }));
    setErrors((current) => withoutError(current, field));
  }

  async function send(): Promise<void> {
    const built = buildAttachRequest(app, draft);
    if (!built.ok) {
      setErrors(built.errors);
      reportInvalid();
      return;
    }
    setErrors({});
    setRefusal(null);
    const { key, request } = built.body;
    const result = await actions.run(
      "attach",
      () => attachResource(app.slug, key, request),
      resourceActionMessage("attach", key),
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
        aria-label="Attach a resource"
      >
        <h3>Attach a resource</h3>
        {draft.fromBundle ? (
          <p className="text-muted-foreground">
            Filled in from the release&apos;s databricks.yml. Check the target
            and choose how the app reaches it before attaching.
          </p>
        ) : null}
        <FormNotices
          title="The resource was not attached"
          errors={errors}
          refusal={refusal}
          onReload={onReload}
        />

        <Field
          label="Key"
          hint="The name the app's env entries use to refer to it."
          error={errors.key}
          isRequired
        >
          {(control) => (
            <Input
              {...control}
              ref={firstFieldRef}
              name="key"
              type="text"
              value={draft.key}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              onChange={(event) => {
                setField("key", event.target.value);
              }}
            />
          )}
        </Field>

        <Field label="Type" error={errors.type} isRequired>
          {(control) => (
            <NativeSelect
              {...control}
              name="type"
              value={draft.type}
              onChange={(event) => {
                const type = event.target.value;
                setDraft((current) => changeDraftType(current, type));
                setErrors((current) => withoutError(current, "type"));
              }}
            >
              {RESOURCE_TYPES.map((candidate) => (
                <NativeSelectOption key={candidate.type} value={candidate.type}>
                  {candidate.label}
                </NativeSelectOption>
              ))}
              {info === null ? (
                <NativeSelectOption value={draft.type}>
                  {draft.type}
                </NativeSelectOption>
              ) : null}
            </NativeSelect>
          )}
        </Field>

        <Field
          label={info?.targetLabel ?? "Target"}
          hint={
            draft.type === "secret"
              ? "A reference to a secret scope and key, such as sales-secrets/api-key. Never type the secret value."
              : undefined
          }
          error={errors.target}
          isRequired
        >
          {(control) => (
            <Input
              {...control}
              name="target"
              type="text"
              value={draft.target}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              onChange={(event) => {
                setField("target", event.target.value);
              }}
            />
          )}
        </Field>

        <Field label="Permission" error={errors.permission} isRequired>
          {(control) => (
            <NativeSelect
              {...control}
              name="permission"
              value={draft.permission}
              onChange={(event) => {
                setField("permission", event.target.value);
              }}
            >
              {info !== null && !info.levels.includes(draft.permission) ? (
                <NativeSelectOption value="">Choose a level</NativeSelectOption>
              ) : null}
              {(info?.levels ?? []).map((level) => (
                <NativeSelectOption key={level} value={level}>
                  {level}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </Field>

        {info?.hasAccessAs === false ? (
          <p className="text-muted-foreground">
            appmux reads a secret itself when the app starts, so no identity is
            chosen.
          </p>
        ) : (
          <>
            <Field
              label="Access as"
              hint="Whose rights the app uses when it reaches this resource."
              error={errors.accessAs}
              isRequired
            >
              {(control) => (
                <NativeSelect
                  {...control}
                  name="access_as"
                  value={draft.accessAs}
                  onChange={(event) => {
                    const { value } = event.target;
                    setDraft((current) => ({
                      ...current,
                      accessAs: isAccessAs(value) ? value : "",
                    }));
                    setErrors((current) => withoutError(current, "accessAs"));
                  }}
                >
                  <NativeSelectOption value="">
                    Choose an identity
                  </NativeSelectOption>
                  {choices.map((choice) => (
                    <NativeSelectOption
                      key={choice.value}
                      value={choice.value}
                      disabled={!choice.isAvailable}
                    >
                      {choice.label}
                      {choice.isAvailable ? "" : " (not available)"}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              )}
            </Field>
            {unavailableChoices.length === 0 ? null : (
              <ul className="text-sm text-muted-foreground">
                {unavailableChoices.map((choice) => (
                  <li key={choice.value}>
                    {choice.label}: {choice.reason}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        <FormActions
          primary={
            <Button
              type="submit"
              variant="primary"
              isBusy={state === "busy"}
              busyLabel="Attaching"
              disabled={state === "blocked"}
            >
              Attach
            </Button>
          }
        >
          <Button onClick={onClose}>Cancel</Button>
        </FormActions>
      </form>
    </Card>
  );
}
