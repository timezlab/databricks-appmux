"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";

import { detachResource } from "@/lib/api";
import type { AppDetail, ResourceView } from "@/lib/api";
import { manageAvailability } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import { resourceActionMessage } from "@/lib/app/outcomes";
import {
  accessAsLabel,
  checkResultLabel,
  detachWarning,
  resourceLevelLabel,
  resourceTypeLabel,
  unusedResourceNote,
} from "@/lib/app/resources";
import type { AttachDraft, ResourceCatalogue } from "@/lib/app/resources";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Lamp } from "@/components/lamp";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { Loading } from "@/components/loading";
import { AddResourcePanel } from "./resources/add-resource-panel";
import { ResourceTypeIcon } from "./resources/resource-type-icon";
import type { ResourceTypesState } from "./resources/use-resource-types";
import type { AppActions } from "./use-app-actions";

export interface ResourcesSectionProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  onReload: () => void;
  /** The catalogue of types, read once for the page. */
  types: ResourceTypesState;
  onRetryTypes: () => void;
  /** The Add panel is open with this draft; `null` when it is closed. */
  draft: AttachDraft | null;
  /** Changes when a different draft is opened, so the panel starts again from it. */
  draftId: number;
  onOpenPanel: () => void;
  onClosePanel: () => void;
}

/**
 * The resources attached to the app: what each reaches, with which permission,
 * as whom, and whether the attach check verified it. Secrets show as their
 * scope and key, never a value. An admin adds one with the Add panel and
 * detaches one after a question that says what still reads it.
 */
export function ResourcesSection({
  app,
  actor,
  actions,
  onReload,
  types,
  onRetryTypes,
  draft,
  draftId,
  onOpenPanel,
  onClosePanel,
}: ResourcesSectionProps) {
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);
  /** The key just added: its row takes the focus once the reloaded app lists it. */
  const pendingFocusKeyRef = useRef<string | null>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  const baseId = useId();
  const gate = manageAvailability(actor);
  const catalogue = types.status === "ready" ? types.catalogue : null;
  const isPanelOpen = draft !== null;

  const rowId = (key: string) => `${baseId}-resource-${key}`;

  // After Add, the keyboard goes to the new row once the reloaded app has it.
  useEffect(() => {
    const key = pendingFocusKeyRef.current;
    if (key === null) return;
    if (!app.resources.some((resource) => resource.key === key)) return;
    document.getElementById(`${baseId}-resource-${key}`)?.focus();
    pendingFocusKeyRef.current = null;
  }, [app.resources, baseId]);

  function closePanel(): void {
    onClosePanel();
    openerRef.current?.focus();
  }

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
        <div className="flex flex-col gap-1">
          <span
            id={rowId(resource.key)}
            tabIndex={-1}
            className="font-mono text-[0.9em] wrap-break-word"
          >
            {resource.key}
          </span>
          <span className="text-xs font-normal wrap-break-word text-muted-foreground">
            Attached <RelativeTime value={resource.attached_at} /> by{" "}
            {resource.attached_by}
          </span>
        </div>
      ),
    },
    {
      // The type and its target in one cell, so a long target gets the width.
      key: "resource",
      header: "Type and target",
      cell: (resource) => (
        <div className="flex flex-col gap-1">
          <span className="inline-flex items-center gap-2">
            <ResourceTypeIcon type={resource.type} />
            {resourceTypeLabel(catalogue, resource.type)}
          </span>
          <span className="font-mono text-[0.9em] wrap-anywhere">
            {resource.target}
          </span>
        </div>
      ),
    },
    {
      key: "permission",
      header: "Permission",
      cell: (resource) => resourceLevelLabel(resource.permission),
    },
    {
      key: "access",
      header: "Access mode",
      cell: (resource) => accessAsLabel(resource.access_as),
    },
    {
      key: "state",
      header: "State",
      cell: (resource) => {
        const unused = unusedResourceNote(app, resource.key);
        return (
          <div className="flex flex-col gap-1">
            {resource.check_result === "ok" ? (
              <Lamp
                state="running"
                label="Verified"
                description={checkResultLabel(resource.check_result)}
              />
            ) : (
              <Lamp
                state="neutral"
                label="Not verified"
                description={checkResultLabel(resource.check_result)}
              />
            )}
            {unused === null ? null : (
              <span className="text-xs text-muted-foreground">{unused}</span>
            )}
          </div>
        );
      },
    },
  ];

  if (gate.isAllowed) {
    columns.push({
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (resource) => {
        const warning = detachWarning(app, resource.key);
        const state = actions.stateOf(`detach:${resource.key}`);
        return (
          <ConfirmButton
            size="small"
            variant="danger"
            label="Detach"
            prompt={`Detach ${resource.key}? ${warning ?? "Nothing in the app's config reads it."} It takes effect at the next start.`}
            confirmLabel={`Yes, detach ${resource.key}`}
            isBusy={state === "busy"}
            busyLabel="Detaching"
            isDisabled={state === "blocked"}
            onConfirm={() => {
              void detach(resource);
            }}
          />
        );
      },
    });
  }

  return (
    <Section
      title="Resources"
      actions={
        gate.isAllowed ? (
          <Button
            ref={openerRef}
            variant={isPanelOpen ? "secondary" : "primary"}
            aria-expanded={isPanelOpen}
            onClick={isPanelOpen ? closePanel : onOpenPanel}
          >
            {isPanelOpen ? "Close" : "Add resource"}
          </Button>
        ) : null
      }
    >
      <div className="flex flex-col gap-4">
        {gate.isAllowed ? null : (
          <p className="text-sm text-muted-foreground">
            Read only: {gate.reason}
          </p>
        )}
        {draft === null ? null : (
          <AddPanelArea
            types={types}
            onRetryTypes={onRetryTypes}
            render={(loaded) => (
              <AddResourcePanel
                key={draftId}
                app={app}
                catalogue={loaded}
                initialDraft={draft}
                actions={actions}
                onClose={closePanel}
                onAdded={(key) => {
                  pendingFocusKeyRef.current = key;
                  onClosePanel();
                }}
                onReload={onReload}
              />
            )}
          />
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
              Add one when the app needs a warehouse, a secret or another
              Databricks object.
            </EmptyState>
          }
        />
      </div>
    </Section>
  );
}

/** The panel once the catalogue is in; a placeholder while it loads, and the failure with a retry. */
function AddPanelArea({
  types,
  onRetryTypes,
  render,
}: {
  types: ResourceTypesState;
  onRetryTypes: () => void;
  render: (catalogue: ResourceCatalogue) => ReactNode;
}) {
  if (types.status === "loading") {
    return <Loading label="Loading the resource types" />;
  }
  if (types.status === "failed") {
    return (
      <ErrorNotice
        error={types.error}
        context="Could not read the resource types"
        onRetry={onRetryTypes}
      />
    );
  }
  return <>{render(types.catalogue)}</>;
}
