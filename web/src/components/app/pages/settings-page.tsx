"use client";

import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import type { AppDetail } from "@/lib/api";
import { appPagePath } from "@/lib/app/params";
import { requestFormAvailability } from "@/lib/app/requests";
import {
  carriedOutcome,
  dropCarriedOutcome,
  settingsAccess,
} from "@/lib/app/settings";
import type { SettingsPartAccess } from "@/lib/app/settings";
import { pollData } from "@/lib/app/view";
import { formatMegabytes } from "@/lib/format";
import { Button } from "@/components/button";
import { LiveRegion } from "@/components/live-region";
import { Notice } from "@/components/notice";
import { RouteStrip } from "@/components/route-strip";
import { Section } from "@/components/section";
import { useAppPageContext } from "../app-context";
import { DeleteAppSection } from "../settings/delete-app-section";
import { AppSettingsForm } from "../settings/settings-form";
import { SettingsRequestForm } from "../settings-request-form";

/**
 * `/app/settings/`: the app's name, slug, source folder, memory budget and
 * owner, and Delete. A platform admin changes them here with one save bar; an
 * app admin reads them and asks a platform admin for a change of the fields
 * (the request form also lives under Requests). Who may do what is
 * `settingsAccess` (lib/app/settings.ts).
 */
export function AppSettingsPage() {
  const { app, actor, actions, requests, reloadAll } = useAppPageContext();
  const access = settingsAccess(actor);
  const path = appPagePath(app.slug, "settings") ?? "";
  // A rename lands here from the old slug's page with what it did (see `carryOutcome`).
  const [arrival] = useState(() => carriedOutcome(path));
  useEffect(() => {
    dropCarriedOutcome(path);
  }, [path]);

  return (
    <div className="flex flex-col gap-8">
      <LiveRegion message={arrival?.title ?? null} />
      {arrival === null ? null : (
        <Notice tone="success" title={arrival.title}>
          {arrival.detail === null ? null : <p>{arrival.detail}</p>}
        </Notice>
      )}
      <Section title="Settings">
        {access.fields.kind === "edit" ? (
          <AppSettingsForm app={app} actions={actions} onReload={reloadAll} />
        ) : (
          <ReadOnlySettings
            app={app}
            fields={access.fields}
            owner={access.owner}
            request={
              <RequestChange
                app={app}
                availability={requestFormAvailability(
                  "settings",
                  app.slug,
                  pollData(requests)?.items ?? null,
                  actor,
                )}
              />
            }
          />
        )}
      </Section>
      <div className="border-t pt-8">
        <DeleteAppSection
          app={app}
          access={access.delete}
          actions={actions}
          onReload={reloadAll}
        />
      </div>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 wrap-break-word">{children}</dd>
    </div>
  );
}

function ReadOnlySettings({
  app,
  fields,
  owner,
  request,
}: {
  app: AppDetail;
  fields: SettingsPartAccess;
  owner: SettingsPartAccess;
  request: ReactNode;
}) {
  return (
    <div className="flex max-w-140 flex-col gap-4">
      <dl className="flex flex-col gap-4">
        <Fact label="Name">{app.display_name}</Fact>
        <Fact label="Slug">
          <RouteStrip slug={app.slug} />
        </Fact>
        <Fact label="Source folder">
          <span className="font-mono text-[0.9em]">{app.source_path}</span>
        </Fact>
        <Fact label="Memory budget">
          {formatMegabytes(app.memory_budget_mb)}
        </Fact>
        <Fact label="Owner">
          {app.owner}
          {owner.kind === "read-only" && fields.kind === "request" ? (
            <span className="block text-sm text-muted-foreground">
              {owner.reason}
            </span>
          ) : null}
        </Fact>
      </dl>
      {fields.kind === "read-only" ? (
        <p className="text-sm text-muted-foreground">{fields.reason}</p>
      ) : (
        request
      )}
    </div>
  );
}

/** An app admin's way to a change: the settings request form, opened in place. */
function RequestChange({
  app,
  availability,
}: {
  app: AppDetail;
  availability: ReturnType<typeof requestFormAvailability>;
}) {
  const { actions, reloadAll } = useAppPageContext();
  const [isOpen, setIsOpen] = useState(false);
  return (
    <div className="flex flex-col gap-4">
      <p className="text-muted-foreground">
        A platform admin changes the name, slug, source folder and memory
        budget. Ask for a change; it waits under Requests until it is answered.
      </p>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Button
          aria-expanded={isOpen}
          disabled={!availability.isAllowed}
          onClick={() => {
            setIsOpen((open) => !open);
          }}
        >
          Request a change
        </Button>
        {availability.isAllowed ? null : (
          <span className="text-sm text-muted-foreground">
            {availability.reason}
          </span>
        )}
      </div>
      {isOpen && availability.isAllowed ? (
        <SettingsRequestForm
          app={app}
          actions={actions}
          onClose={() => {
            setIsOpen(false);
          }}
          onReload={reloadAll}
        />
      ) : null}
    </div>
  );
}
