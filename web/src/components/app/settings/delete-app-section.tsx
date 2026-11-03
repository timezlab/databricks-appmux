"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { deleteApp } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import { carryOutcome, isDeleteConfirmed } from "@/lib/app/settings";
import type { SettingsPartAccess } from "@/lib/app/settings";
import { PLATFORM_APPS_PATH } from "@/lib/platform/sections";
import { Button } from "@/components/button";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import { Section } from "@/components/section";
import { Input } from "@/components/ui/input";
import type { AppActions } from "../use-app-actions";

const DELETE_KEY = "settings:delete";

export interface DeleteAppSectionProps {
  app: AppDetail;
  access: SettingsPartAccess;
  actions: AppActions;
  onReload: () => void;
}

/**
 * `DELETE /apps/{app}` (design 9.1, `app.delete`; what goes with it: design
 * 13.2, "Delete an app"). The button opens a question in place that asks for the
 * slug typed out, so a press on the wrong app's page does nothing. Deleting is
 * not a request in v1 (design 9.4), so anyone else only reads why it is not
 * theirs to do.
 */
export function DeleteAppSection({
  app,
  access,
  actions,
  onReload,
}: DeleteAppSectionProps) {
  const router = useRouter();
  const [isAsking, setIsAsking] = useState(false);
  const [typed, setTyped] = useState("");
  const [refusal, setRefusal] = useState<{ error: unknown } | null>(null);
  const fieldRef = useRef<HTMLInputElement>(null);
  const openButtonRef = useRef<HTMLButtonElement>(null);
  const wasAskingRef = useRef(false);
  const state = actions.stateOf(DELETE_KEY);
  const isConfirmed = isDeleteConfirmed(typed, app.slug);

  useEffect(() => {
    if (isAsking) {
      fieldRef.current?.focus();
    } else if (wasAskingRef.current) {
      openButtonRef.current?.focus();
    }
    wasAskingRef.current = isAsking;
  }, [isAsking]);

  function close(): void {
    setIsAsking(false);
    setTyped("");
  }

  async function remove(): Promise<void> {
    setRefusal(null);
    const slug = app.slug;
    const result = await actions.run(
      DELETE_KEY,
      async () => {
        await deleteApp(slug);
        // This page answers 404 from now on: land on the platform's list of apps.
        carryOutcome(PLATFORM_APPS_PATH, {
          title: `Deleted ${slug}`,
          detail:
            "Its route is closed; its configuration, releases, resources, secrets and requests are deleted with it.",
        });
        router.push(PLATFORM_APPS_PATH);
      },
      `Deleted ${slug}`,
    );
    if (!result.ok) setRefusal({ error: result.error });
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (isConfirmed) void remove();
  }

  return (
    <Section title="Delete app">
      <p className="max-w-140">
        Deleting stops {app.slug} and unregisters it: its route closes, and its
        configuration, releases, resources, secrets and requests are deleted
        with it. Its metrics and the audit log stay. This cannot be undone.
      </p>
      {access.kind !== "edit" ? (
        <p className="text-sm text-muted-foreground">
          {access.kind === "read-only"
            ? access.reason
            : "Deleting is not a request; ask a platform admin."}
        </p>
      ) : isAsking ? (
        <form
          onSubmit={handleSubmit}
          noValidate
          className="flex max-w-140 flex-col gap-4 rounded-md border-2 border-tone-danger-border bg-tone-danger-bg p-4 text-tone-danger-text"
          aria-label={`Delete ${app.slug}`}
        >
          <Field
            label={`Type ${app.slug} to delete it`}
            hint="The slug as it appears in the app's address."
          >
            {(control) => (
              <Input
                {...control}
                ref={fieldRef}
                name="confirm_slug"
                type="text"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                value={typed}
                onKeyDown={(event) => {
                  if (event.key === "Escape") close();
                }}
                onChange={(event) => {
                  setTyped(event.target.value);
                }}
              />
            )}
          </Field>
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={close} disabled={state === "busy"}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="danger-filled"
              isBusy={state === "busy"}
              busyLabel="Deleting"
              disabled={!isConfirmed || state === "blocked"}
            >
              Delete {app.slug}
            </Button>
            {isConfirmed ? null : (
              <span className="text-sm">
                Type the slug above to turn on Delete.
              </span>
            )}
          </div>
        </form>
      ) : (
        <div>
          <Button
            ref={openButtonRef}
            variant="danger"
            onClick={() => {
              setIsAsking(true);
            }}
          >
            Delete {app.slug}...
          </Button>
        </div>
      )}
      {refusal === null ? null : (
        <ErrorNotice
          error={refusal.error}
          context={`Could not delete ${app.slug}`}
          onReload={onReload}
        />
      )}
    </Section>
  );
}
