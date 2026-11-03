"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { attachResource } from "@/lib/api";
import type { AccessAs, AppDetail } from "@/lib/api";
import { withoutError } from "@/lib/app/forms";
import type { FormErrors } from "@/lib/app/forms";
import { resourceActionMessage } from "@/lib/app/outcomes";
import { appPagePath } from "@/lib/app/params";
import {
  accessModeChoices,
  buildAttachRequest,
  chooseDraftObject,
  chooseDraftType,
  editDraftKey,
  findResourceType,
  resourceLevelLabel,
  resourceTypeLabel,
  typeDraftTarget,
} from "@/lib/app/resources";
import type {
  AccessModeChoice,
  AttachDraft,
  AttachField,
  ResourceCatalogue,
} from "@/lib/app/resources";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import { FORM_STACK_CLASSES, FormActions } from "@/components/form-layout";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FormNotices } from "../form-notices";
import type { AppActions } from "../use-app-actions";
import { useFocusFirstInvalid } from "../use-form-focus";
import { ResourceObjectField } from "./resource-object-field";
import { ResourceTypeIcon } from "./resource-type-icon";

export interface AddResourcePanelProps {
  app: AppDetail;
  catalogue: ResourceCatalogue;
  /** What the panel holds when it opens: empty, or a requirement filled in. */
  initialDraft: AttachDraft;
  actions: AppActions;
  onClose: () => void;
  /** The resource was attached under this key. */
  onAdded: (key: string) => void;
  onReload: () => void;
}

const ACCESS_MODES: readonly AccessAs[] = ["obo", "own_sp", "shared_sp"];

function isAccessAs(value: string): value is AccessAs {
  return ACCESS_MODES.some((mode) => mode === value);
}

const SECRET_HINT =
  "A scope, then a key. appmux reads the value when the app starts; never type the value here.";

/**
 * Add a resource, step by step as in the Databricks console: the type (from
 * the server's catalogue), the object (from what the person can see), the
 * permission, how the app reaches it, and the key, filled in and editable.
 * The attach check (the person's own token reaching the object) runs on the
 * server; its refusal is shown in words and the panel keeps what was chosen.
 */
export function AddResourcePanel({
  app,
  catalogue,
  initialDraft,
  actions,
  onClose,
  onAdded,
  onReload,
}: AddResourcePanelProps) {
  const [draft, setDraft] = useState<AttachDraft>(initialDraft);
  const [errors, setErrors] = useState<FormErrors>({});
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const state = actions.stateOf("attach");
  const entry = findResourceType(catalogue, draft.type);
  const takenKeys = app.resources.map((resource) => resource.key);
  const modes = entry === null ? [] : accessModeChoices(app, entry);

  // The panel opens from a button press: the keyboard goes to its first field.
  useEffect(() => {
    formRef.current
      ?.querySelector<HTMLElement>('button[role="combobox"], input')
      ?.focus();
  }, [formRef]);

  function update(field: AttachField, next: AttachDraft): void {
    setDraft(next);
    setErrors((current) => withoutError(current, field));
  }

  async function send(): Promise<void> {
    const built = buildAttachRequest(app, draft, entry);
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
      onAdded(key);
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
        aria-label="Add a resource"
      >
        <h3>Add a resource</h3>
        {draft.fromBundle ? (
          <p className="text-muted-foreground">
            Filled in from the release&apos;s databricks.yml. Check the object
            and choose how the app reaches it before adding it.
          </p>
        ) : null}
        <FormNotices
          title="The resource was not attached"
          errors={errors}
          refusal={refusal}
          onReload={onReload}
        />

        <Field label="Type" error={errors.type} isRequired>
          {(control) => (
            <Select
              value={entry === null ? "" : entry.type}
              onValueChange={(type) => {
                const chosen = findResourceType(catalogue, type);
                if (chosen !== null) {
                  update("type", chooseDraftType(draft, chosen, takenKeys));
                }
              }}
            >
              <SelectTrigger {...control} className="w-full max-w-xl">
                <SelectValue placeholder="Choose a type">
                  {entry === null ? null : (
                    <>
                      <ResourceTypeIcon type={entry.type} />
                      {resourceTypeLabel(catalogue, entry.type)}
                    </>
                  )}
                </SelectValue>
              </SelectTrigger>
              <SelectContent
                label="Type"
                searchPlaceholder="Search types"
                emptyMessage="No type matches."
              >
                {catalogue.map((candidate) => (
                  <SelectItem
                    key={candidate.type}
                    value={candidate.type}
                    textValue={resourceTypeLabel(catalogue, candidate.type)}
                  >
                    <span className="flex items-start gap-2">
                      <ResourceTypeIcon
                        type={candidate.type}
                        className="mt-0.5"
                      />
                      <span className="flex flex-col">
                        <span>
                          {resourceTypeLabel(catalogue, candidate.type)}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {candidate.description}
                        </span>
                      </span>
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </Field>

        {entry === null ? null : (
          <>
            <Field
              label={resourceTypeLabel(catalogue, entry.type)}
              hint={entry.type === "secret" ? SECRET_HINT : undefined}
              error={errors.target}
              isRequired
            >
              {(control) => (
                <ResourceObjectField
                  key={entry.type}
                  {...control}
                  entry={entry}
                  target={draft.target}
                  objectName={draft.objectName}
                  onPick={(suggestion) => {
                    update(
                      "target",
                      chooseDraftObject(draft, suggestion, takenKeys),
                    );
                  }}
                  onType={(target) => {
                    update("target", typeDraftTarget(draft, target, takenKeys));
                  }}
                />
              )}
            </Field>

            <Field
              label="Permission"
              hint="What the app may do with it."
              error={errors.permission}
              isRequired
            >
              {(control) => (
                <Select
                  value={draft.permission}
                  onValueChange={(permission) => {
                    update("permission", { ...draft, permission });
                  }}
                >
                  <SelectTrigger {...control} className="w-full max-w-xs">
                    <SelectValue placeholder="Choose a permission" />
                  </SelectTrigger>
                  <SelectContent label="Permission">
                    {entry.permissions.map((level) => (
                      <SelectItem
                        key={level}
                        value={level}
                        textValue={resourceLevelLabel(level)}
                      >
                        {resourceLevelLabel(level)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </Field>

            {entry.access_as.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Access mode: none. appmux reads a secret itself when the app
                starts.
              </p>
            ) : (
              <div className="flex flex-col gap-2">
                <Field
                  label="Access mode"
                  hint="Whose rights the app uses when it reaches this resource."
                  error={errors.accessAs}
                  isRequired
                >
                  {(control) => (
                    <Select
                      value={draft.accessAs}
                      onValueChange={(value) => {
                        if (isAccessAs(value)) {
                          update("accessAs", { ...draft, accessAs: value });
                        }
                      }}
                    >
                      <SelectTrigger {...control} className="w-full max-w-xl">
                        <SelectValue placeholder="Choose how the app reaches it" />
                      </SelectTrigger>
                      <SelectContent label="Access mode">
                        {modes.map((mode) => (
                          <SelectItem
                            key={mode.value}
                            value={mode.value}
                            textValue={mode.label}
                            disabled={!mode.isAvailable}
                          >
                            <span className="flex flex-col">
                              <span>{mode.label}</span>
                              {mode.reason === null ? null : (
                                <span className="text-xs text-muted-foreground">
                                  {mode.reason}
                                </span>
                              )}
                            </span>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </Field>
                <AccessModeHelp modes={modes} slug={app.slug} />
              </div>
            )}

            <Field
              label="Key"
              hint="The name the app's env entries use for it (valueFrom). Filled from the type and the object; change it if the app expects another."
              error={errors.key}
              isRequired
            >
              {(control) => (
                <Input
                  {...control}
                  name="key"
                  type="text"
                  value={draft.key}
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  className="max-w-xs font-mono"
                  onChange={(event) => {
                    update("key", editDraftKey(draft, event.target.value));
                  }}
                />
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
              busyLabel="Adding"
              disabled={state === "blocked"}
            >
              Add
            </Button>
          }
        >
          <Button onClick={onClose}>Cancel</Button>
        </FormActions>
      </form>
    </Card>
  );
}

/** The modes that cannot be chosen, each with its reason and the way to get it when there is one. */
function AccessModeHelp({
  modes,
  slug,
}: {
  modes: readonly AccessModeChoice[];
  slug: string;
}) {
  const unavailable = modes.filter((mode) => !mode.isAvailable);
  if (unavailable.length === 0) return null;
  const authorizationPath = appPagePath(slug, "authorization");
  return (
    <ul className="flex flex-col gap-1 text-sm text-muted-foreground">
      {unavailable.map((mode) => (
        <li key={mode.value}>
          <span className="font-medium text-foreground">{mode.label}:</span>{" "}
          {mode.reason}
          {mode.howToGet === null ? null : <> {mode.howToGet}</>}
          {mode.howToGet !== null &&
          mode.value !== "own_sp" &&
          authorizationPath !== null ? (
            <>
              {" "}
              <Link href={authorizationPath}>Open Authorization</Link>
            </>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
