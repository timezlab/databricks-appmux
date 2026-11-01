"use client";

import { useEffect, useId, useRef } from "react";
import type { FormEvent } from "react";

import { putAppEgress, setOwner, updateApp } from "@/lib/api";
import type { PlatformApp } from "@/lib/api";
import {
  appFieldsFormValues,
  appFieldsKey,
  appOwnerKey,
  parseAppFieldsForm,
  renameOutcome,
  parseOwnerForm,
} from "@/lib/platform/apps";
import type { ActionOutcome } from "@/lib/platform/action";
import type { AppFieldsValues } from "@/lib/platform/apps";
import { Card } from "@/components/ui/card";
import { AppLink } from "@/components/app-link";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { Input } from "@/components/ui/input";
import { ActionResult } from "./action-result";
import { EgressEditor } from "./egress-editor";
import { FormSummary } from "./form-summary";
import { useKeyedState } from "./use-keyed-state";
import { useWriteAction } from "./use-write-action";

// The base styles stop at <h3>; the groups inside a panel are one step below.
const SUBHEADING = "text-base font-bold";

/**
 * One app's platform fields, owner and egress rules. Every form here starts
 * from the values the last poll sent and keeps what the person typed until
 * those values change; each save sends the latest `apps` version.
 */
export function AppPanel({
  app,
  otherSlugs,
  onRenamed,
  onClose,
}: {
  app: PlatformApp;
  otherSlugs: readonly string[];
  onRenamed: (from: string, to: string, outcome: ActionOutcome) => void;
  onClose: () => void;
}) {
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);

  // The panel opens below the table; keyboard focus follows it.
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <Card asChild>
      <section className="flex flex-col gap-4" aria-labelledby={headingId}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3
            id={headingId}
            ref={headingRef}
            tabIndex={-1}
            className="wrap-break-word"
          >
            {app.slug}
          </h3>
          <div className="flex flex-wrap items-center gap-2">
            <AppLink slug={app.slug} buttonVariant="secondary" size="small">
              Open {app.slug}
            </AppLink>
            <Button size="small" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>

        <AppFieldsForm
          app={app}
          otherSlugs={otherSlugs}
          onRenamed={onRenamed}
        />
        <OwnerForm app={app} />

        <div className="flex flex-col gap-2">
          <h4 className={SUBHEADING}>Egress rules</h4>
          <EgressEditor
            name={`Egress rules of ${app.slug}`}
            scope="app"
            source={{ rules: app.egress, version: app.version }}
            save={async (rules, version) => {
              const detail = await putAppEgress(app.slug, {
                version: version ?? app.version,
                rules,
              });
              return { rules: detail.egress, version: detail.version };
            }}
            savedTitle={`Saved the egress rules of ${app.slug}`}
          />
        </div>
      </section>
    </Card>
  );
}

function AppFieldsForm({
  app,
  otherSlugs,
  onRenamed,
}: {
  app: PlatformApp;
  otherSlugs: readonly string[];
  onRenamed: (from: string, to: string, outcome: ActionOutcome) => void;
}) {
  const initial = appFieldsFormValues(app);
  const key = appFieldsKey(app);
  const [values, setValues] = useKeyedState<AppFieldsValues>(initial, key);
  // Messages stay hidden until the first Save, then follow the typing.
  const [hasTried, setHasTried] = useKeyedState(false, key);
  const action = useWriteAction();

  const parsed = parseAppFieldsForm(app, values, otherSlugs);
  const errors = hasTried && !parsed.ok ? parsed.errors : {};
  const isChanged = JSON.stringify(values) !== JSON.stringify(initial);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setHasTried(true);
    if (!parsed.ok) return;
    const body = parsed.value;
    void action.run(async () => {
      const detail = await updateApp(app.slug, body);
      if (detail.slug === app.slug) {
        return { tone: "success", title: `Saved ${detail.slug}` };
      }
      const outcome = renameOutcome(app.slug, detail.slug);
      // This form is replaced when the renamed app arrives; the list shows the outcome.
      onRenamed(app.slug, detail.slug, outcome);
      return outcome;
    });
  }

  return (
    <form className={FORM_STACK_CLASSES} onSubmit={handleSubmit}>
      <h4 className={SUBHEADING}>Platform fields</h4>
      <FormSummary errors={errors} />
      <FieldGrid>
        <Field
          label="Slug"
          hint="Changing it changes the app's URL prefix; the old URL answers 404."
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
        <Field label="Display name" error={errors.display_name}>
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
          hint="A workspace path starting with /Workspace/"
          error={errors.source_path}
        >
          {(control) => (
            <Input
              {...control}
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={values.source_path}
              onChange={(event) => {
                setValues({ ...values, source_path: event.target.value });
              }}
            />
          )}
        </Field>
        <Field
          label="Memory budget (MB)"
          hint="Whole megabytes. Counted against the slot while the app runs."
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
                setValues({ ...values, memory_budget_mb: event.target.value });
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
            busyLabel="Saving"
            disabled={!isChanged}
          >
            Save fields
          </Button>
        }
      />
      <ActionResult action={action} context={`Could not save ${app.slug}`} />
    </form>
  );
}

function OwnerForm({ app }: { app: PlatformApp }) {
  const key = appOwnerKey(app);
  const [owner, setOwnerText] = useKeyedState(app.owner, key);
  const [hasTried, setHasTried] = useKeyedState(false, key);
  const action = useWriteAction();

  const parsed = parseOwnerForm(app, owner);
  const error = hasTried && !parsed.ok ? parsed.errors.owner : undefined;
  const isChanged = owner !== app.owner;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setHasTried(true);
    if (!parsed.ok) return;
    const body = parsed.value;
    void action.run(async () => {
      const detail = await setOwner(app.slug, body);
      return {
        tone: "success",
        title: `Changed the owner of ${detail.slug}`,
        detail: `The owner is now ${detail.owner}.`,
      };
    });
  }

  return (
    <form className={FORM_STACK_CLASSES} onSubmit={handleSubmit}>
      <h4 className={SUBHEADING}>Owner</h4>
      <Field label="Owner" hint="The owner's email address." error={error}>
        {(control) => (
          <Input
            {...control}
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={owner}
            onChange={(event) => {
              setOwnerText(event.target.value);
            }}
          />
        )}
      </Field>
      <FormActions
        primary={
          <Button
            type="submit"
            variant="primary"
            isBusy={action.isBusy}
            busyLabel="Saving"
            disabled={!isChanged}
          >
            Change owner
          </Button>
        }
      />
      <ActionResult
        action={action}
        context={`Could not change the owner of ${app.slug}`}
      />
    </form>
  );
}
