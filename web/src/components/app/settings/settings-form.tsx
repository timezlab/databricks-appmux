"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { setOwner, updateApp } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import { appPagePath } from "@/lib/app/params";
import {
  NODE_MEMORY_MIN_MB,
  appSettingsFormValues,
  appSettingsKey,
  carryOutcome,
  nodeMemoryHint,
  parseSettingsForm,
  renameMessage,
  renameWarning,
} from "@/lib/app/settings";
import type { AppSettingsValues, SettingsChange } from "@/lib/app/settings";
import { Button } from "@/components/button";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import { SourceFolderInput } from "@/components/folder-browser";
import { Notice } from "@/components/notice";
import { Input } from "@/components/ui/input";
import { useKeyedState } from "@/components/platform/use-keyed-state";
import { FormNotices } from "../form-notices";
import type { AppActions } from "../use-app-actions";
import { useFocusFirstInvalid } from "../use-form-focus";

const SAVE_KEY = "settings:save";
const REASON_OTHER_WRITE = "Another change is running; wait for it to finish.";
const OWNER_NOT_CHANGED = "The fields were saved; the owner was not changed";

export interface AppSettingsFormProps {
  app: AppDetail;
  actions: AppActions;
  /** Reads the app again: offered after a 409, and run when only part of a save went through. */
  onReload: () => void;
}

/**
 * A platform admin's form for the app's platform fields and its owner, with one
 * save bar for all of them (design 9.1: `app.edit_platform_fields`,
 * `app.set_owner`). It starts from the polled app and keeps what the person
 * typed until a field the form shows changes on the server; Save reads the
 * latest version. A new slug moves the app's address, so Save asks first and
 * then moves this page to the new slug.
 */
export function AppSettingsForm({
  app,
  actions,
  onReload,
}: AppSettingsFormProps) {
  const router = useRouter();
  const key = appSettingsKey(app);
  const initial = appSettingsFormValues(app);
  const [values, setValues] = useKeyedState<AppSettingsValues>(initial, key);
  // Messages stay hidden until the first Save, then follow the typing.
  const [hasTried, setHasTried] = useKeyedState(false, key);
  // The slug the person was asked to confirm the move to; a new edit asks again.
  const [moveAsked, setMoveAsked] = useKeyedState<string | null>(null, key);
  const [refusal, setRefusal] = useState<{
    error: unknown;
    context: string;
  } | null>(null);
  const { formRef, reportInvalid } = useFocusFirstInvalid();
  const keepButtonRef = useRef<HTMLButtonElement>(null);
  const saveButtonRef = useRef<HTMLButtonElement>(null);
  const wasAskingRef = useRef(false);

  const state = actions.stateOf(SAVE_KEY);
  const parsed = parseSettingsForm(app, values, []);
  const errors = hasTried && !parsed.ok ? parsed.errors : {};
  const isChanged = JSON.stringify(values) !== JSON.stringify(initial);
  const newSlug = parsed.ok ? (parsed.value.fields?.slug ?? null) : null;
  // The new slug while the save bar asks about the move; `null` otherwise.
  const askedMove = newSlug !== null && moveAsked === newSlug ? newSlug : null;
  const isAskingMove = askedMove !== null;
  const memoryHint = nodeMemoryHint(app.runtime, values.memory_budget_mb);

  // The question takes the keyboard to its safe answer; answering gives it back to Save.
  useEffect(() => {
    if (isAskingMove) {
      keepButtonRef.current?.focus();
    } else if (wasAskingRef.current) {
      saveButtonRef.current?.focus();
    }
    wasAskingRef.current = isAskingMove;
  }, [isAskingMove]);

  function setField(field: keyof AppSettingsValues, value: string): void {
    setValues((current) => ({ ...current, [field]: value }));
  }

  function discard(): void {
    setValues(initial);
    setHasTried(false);
    setMoveAsked(null);
    setRefusal(null);
  }

  async function save(change: SettingsChange): Promise<void> {
    setRefusal(null);
    setMoveAsked(null);
    const from = app.slug;
    let isFieldsSaved = false;
    const result = await actions.run(
      SAVE_KEY,
      async () => {
        let slug = from;
        let version = app.version;
        if (change.fields !== null) {
          const detail = await updateApp(from, change.fields);
          isFieldsSaved = true;
          slug = detail.slug;
          version = detail.version;
        }
        const movedPath = slug === from ? null : appPagePath(slug, "settings");
        if (movedPath === null) {
          if (change.owner !== null) {
            await setOwner(slug, { version, owner: change.owner });
          }
          return slug;
        }
        // The old slug answers 404 from now on, so this page goes where the app
        // is at once, without waiting for the owner change: read again here, the
        // app would be "not found" and a refusal of the owner never seen. The
        // page at the new slug shows how the owner change ended.
        const ownerChange =
          change.owner === null
            ? undefined
            : setOwner(slug, { version, owner: change.owner }).then(
                () => null,
                (error: unknown) => ({ error, context: OWNER_NOT_CHANGED }),
              );
        carryOutcome(movedPath, {
          title: `Renamed ${from} to ${slug}`,
          detail: renameMessage(from, slug),
          followUp: ownerChange,
        });
        router.replace(movedPath);
        // One write at a time holds until the owner change has answered too.
        await ownerChange;
        return slug;
      },
      (slug) =>
        slug === from
          ? savedMessage(from, change)
          : `Renamed ${from} to ${slug}`,
    );
    if (result.ok) return;
    if (isFieldsSaved) {
      // The fields went through and the owner did not: show what the server now holds.
      onReload();
      setRefusal({ error: result.error, context: OWNER_NOT_CHANGED });
    } else {
      setRefusal({
        error: result.error,
        context: "The settings were not saved",
      });
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setHasTried(true);
    if (!parsed.ok) {
      reportInvalid();
      return;
    }
    if (newSlug !== null) {
      // A new address is confirmed in the save bar, never by Enter in a field.
      if (!isAskingMove) setMoveAsked(newSlug);
      return;
    }
    void save(parsed.value);
  }

  return (
    <form
      ref={formRef}
      onSubmit={handleSubmit}
      noValidate
      className="flex max-w-140 flex-col gap-4"
      aria-label="Settings"
    >
      <FormNotices
        title="The settings were not saved"
        errors={errors}
        refusal={null}
        onReload={onReload}
      />
      <Field
        label="Name"
        hint="What people see on the launcher."
        error={errors.display_name}
      >
        {(control) => (
          <Input
            {...control}
            name="display_name"
            type="text"
            autoComplete="off"
            value={values.display_name}
            onChange={(event) => {
              setField("display_name", event.target.value);
            }}
          />
        )}
      </Field>
      <Field
        label="Slug"
        hint={`The app opens at /${app.slug}/. A new slug moves that address; Save asks before it does. 1 to 31 lower-case letters, digits or hyphens.`}
        error={errors.slug}
      >
        {(control) => (
          <Input
            {...control}
            name="slug"
            type="text"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            value={values.slug}
            onChange={(event) => {
              setField("slug", event.target.value);
            }}
          />
        )}
      </Field>
      <Field
        label="Source folder"
        hint="The workspace folder the app is read from, starting with /Workspace/. Type it, or pick it with Browse. A new folder is read at the next Update."
        error={errors.source_path}
      >
        {(control) => (
          <SourceFolderInput
            control={control}
            name="source_path"
            value={values.source_path}
            onValueChange={(value) => {
              setField("source_path", value);
            }}
            canBrowse
            onUseMemory={(memoryMb) => {
              setField("memory_budget_mb", String(memoryMb));
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
            name="memory_budget_mb"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={values.memory_budget_mb}
            onChange={(event) => {
              setField("memory_budget_mb", event.target.value);
            }}
          />
        )}
      </Field>
      {memoryHint === null ? null : (
        <Notice tone="warning" title="Node.js needs more memory">
          <p>{memoryHint}</p>
          <div className="pt-2">
            <Button
              size="small"
              onClick={() => {
                setField("memory_budget_mb", String(NODE_MEMORY_MIN_MB));
              }}
            >
              Use {NODE_MEMORY_MIN_MB} MB
            </Button>
          </div>
        </Notice>
      )}
      <Field
        label="Owner"
        hint="The owner's email address. The owner always manages the app."
        error={errors.owner}
      >
        {(control) => (
          <Input
            {...control}
            name="owner"
            type="email"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            value={values.owner}
            onChange={(event) => {
              setField("owner", event.target.value);
            }}
          />
        )}
      </Field>

      {refusal === null ? null : (
        <ErrorNotice
          error={refusal.error}
          context={refusal.context}
          onReload={onReload}
        />
      )}

      {isChanged || state === "busy" ? (
        <div
          role="region"
          aria-label="Unsaved changes"
          className="sticky bottom-0 z-10 flex flex-col gap-4 border-t-2 border-t-tone-info-border bg-card py-4 sm:flex-row sm:items-center sm:justify-between"
        >
          <div className="flex min-w-0 flex-1 flex-col gap-1" role="status">
            <p className="font-semibold">
              {state === "busy"
                ? "Saving"
                : askedMove !== null
                  ? `Move ${app.slug} to /${askedMove}/?`
                  : "Unsaved changes"}
            </p>
            {askedMove === null ? null : (
              <p className="text-sm">{renameWarning(app.slug, askedMove)}</p>
            )}
            {state === "blocked" ? (
              <p className="text-sm text-muted-foreground">
                {REASON_OTHER_WRITE}
              </p>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-2 sm:justify-end">
            {askedMove !== null ? (
              <>
                <Button
                  ref={keepButtonRef}
                  onClick={() => {
                    setMoveAsked(null);
                  }}
                >
                  Keep /{app.slug}/
                </Button>
                <Button
                  variant="danger-filled"
                  disabled={state === "blocked"}
                  onClick={() => {
                    if (parsed.ok) void save(parsed.value);
                  }}
                >
                  Yes, move to /{askedMove}/
                </Button>
              </>
            ) : (
              <>
                <Button onClick={discard} disabled={state === "busy"}>
                  Discard
                </Button>
                <Button
                  ref={saveButtonRef}
                  type="submit"
                  variant="primary"
                  isBusy={state === "busy"}
                  busyLabel="Saving"
                  disabled={state === "blocked"}
                >
                  Save changes
                </Button>
              </>
            )}
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          Nothing changed yet. Change a field and a bar with Save appears here.
        </p>
      )}
    </form>
  );
}

function savedMessage(slug: string, change: SettingsChange): string {
  if (change.fields === null && change.owner !== null) {
    return `Changed the owner of ${slug} to ${change.owner}`;
  }
  return change.owner === null
    ? `Saved the settings of ${slug}`
    : `Saved the settings of ${slug}; the owner is now ${change.owner}`;
}
