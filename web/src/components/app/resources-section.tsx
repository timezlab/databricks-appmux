"use client";

import { useState } from "react";

import { detachResource } from "@/lib/api";
import type { AppDetail, ResourceView } from "@/lib/api";
import { manageAvailability } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import { resourceActionMessage } from "@/lib/app/outcomes";
import {
  accessAsLabel,
  checkResultLabel,
  detachWarning,
  resourceTypeLabel,
} from "@/lib/app/resources";
import type { AttachDraft } from "@/lib/app/resources";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { AttachResourceForm } from "./attach-resource-form";
import type { AppActions } from "./use-app-actions";

export interface ResourcesSectionProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  onReload: () => void;
  /** The attach form is open with this draft; `null` when it is closed. */
  draft: AttachDraft | null;
  /** Changes when a different draft is opened, so the form starts again from it. */
  draftId: number;
  onOpenForm: () => void;
  onCloseForm: () => void;
}

/** The resources attached to the app: what each reaches, as whom, and who attached it. Secrets show as references. */
export function ResourcesSection({
  app,
  actor,
  actions,
  onReload,
  draft,
  draftId,
  onOpenForm,
  onCloseForm,
}: ResourcesSectionProps) {
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);
  const gate = manageAvailability(actor);

  async function detach(resource: ResourceView): Promise<void> {
    setFailure(null);
    const result = await actions.run(
      `detach:${resource.key}`,
      () => detachResource(app.slug, resource.key),
      resourceActionMessage("detach", resource.key),
    );
    if (!result.ok) setFailure({ error: result.error });
  }

  const columns: DataTableColumn<ResourceView>[] = [
    {
      key: "key",
      header: "Key",
      isRowHeader: true,
      cell: (resource) => (
        <span className="font-mono text-[0.9em] wrap-break-word">
          {resource.key}
        </span>
      ),
    },
    {
      key: "target",
      header: "Resource",
      cell: (resource) => (
        <div className="flex flex-col gap-1">
          <div className="wrap-break-word">
            {resourceTypeLabel(resource.type)}:{" "}
            <span className="font-mono text-[0.9em]">{resource.target}</span>
          </div>
          <div className="text-muted-foreground">{resource.permission}</div>
        </div>
      ),
    },
    {
      key: "access",
      header: "Access as",
      cell: (resource) => (
        <div className="flex flex-col gap-1">
          <div>{accessAsLabel(resource.access_as)}</div>
          <div className="text-muted-foreground">
            {checkResultLabel(resource.check_result)}
          </div>
        </div>
      ),
    },
    {
      key: "attached",
      header: "Attached",
      cell: (resource) => (
        <div className="flex flex-col gap-1">
          <RelativeTime value={resource.attached_at} />
          <span className="wrap-break-word text-muted-foreground">
            by {resource.attached_by}
          </span>
        </div>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (resource) => {
        const warning = detachWarning(app, resource.key);
        const state = actions.stateOf(`detach:${resource.key}`);
        return (
          <ConfirmButton
            size="small"
            label="Detach"
            prompt={`Detach ${resource.key}? ${warning ?? "Nothing in the app's config reads it."} It takes effect at the next start.`}
            confirmLabel={`Yes, detach ${resource.key}`}
            isBusy={state === "busy"}
            busyLabel="Detaching"
            isDisabled={!gate.isAllowed || state === "blocked"}
            onConfirm={() => {
              void detach(resource);
            }}
          />
        );
      },
    },
  ];

  return (
    <Section
      title="Resources"
      actions={
        <Button
          aria-expanded={draft !== null}
          disabled={!gate.isAllowed}
          onClick={draft === null ? onOpenForm : onCloseForm}
        >
          {draft === null ? "Attach a resource" : "Close the form"}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        {gate.isAllowed ? null : (
          <p className="text-sm text-muted-foreground">
            Attach and Detach: {gate.reason}
          </p>
        )}
        {failure === null ? null : (
          <ErrorNotice
            error={failure.error}
            context="Could not detach the resource"
            onReload={onReload}
          />
        )}
        <DataTable
          caption="Resources attached to this app"
          isCaptionHidden
          columns={columns}
          rows={app.resources}
          rowKey={(resource) => resource.key}
          empty={
            <EmptyState title="No resources attached">
              Attach a resource when the app needs a warehouse, a secret or
              another Databricks object.
            </EmptyState>
          }
        />
        {draft === null ? null : (
          <AttachResourceForm
            key={draftId}
            app={app}
            initialDraft={draft}
            actions={actions}
            onClose={onCloseForm}
            onReload={onReload}
          />
        )}
      </div>
    </Section>
  );
}
