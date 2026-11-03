"use client";

import type { AppDetail } from "@/lib/api";
import { manageAvailability } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import {
  accessAsLabel,
  attachDraftFromOffer,
  attachDraftFromRequirement,
  requirementRows,
  requirementsSummary,
  resourceLevelLabel,
  resourceTypeLabel,
} from "@/lib/app/resources";
import type {
  AttachDraft,
  RequirementRow,
  ResourceCatalogue,
} from "@/lib/app/resources";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { Lamp } from "@/components/lamp";
import { Section } from "@/components/section";
import { ResourceTypeIcon } from "./resources/resource-type-icon";

export interface RequirementsSectionProps {
  app: AppDetail;
  actor: Actor | null;
  /** The catalogue of types, `null` while it loads (types show by their wire name then). */
  catalogue: ResourceCatalogue | null;
  /** Opens the Add panel with this draft filled in. */
  onAdd: (draft: AttachDraft) => void;
}

function TypeName({
  catalogue,
  type,
}: {
  catalogue: ResourceCatalogue | null;
  type: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <ResourceTypeIcon type={type} />
      {resourceTypeLabel(catalogue, type)}
    </span>
  );
}

function RequirementDetail({
  row,
  catalogue,
}: {
  row: RequirementRow;
  catalogue: ResourceCatalogue | null;
}) {
  if (row.resource !== null) {
    const { resource } = row;
    return (
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-x-2 wrap-break-word">
          <TypeName catalogue={catalogue} type={resource.type} />
          <span className="font-mono text-[0.9em] break-all">
            {resource.target}
          </span>
        </div>
        <div className="text-muted-foreground">
          {resourceLevelLabel(resource.permission)},{" "}
          {accessAsLabel(resource.access_as).toLowerCase()}
        </div>
      </div>
    );
  }
  if (row.offer !== null) {
    const { offer } = row;
    return (
      <div className="flex flex-col gap-1">
        <div className="text-muted-foreground">
          The release&apos;s databricks.yml offers:
        </div>
        <div className="flex flex-wrap items-center gap-x-2 wrap-break-word">
          <TypeName catalogue={catalogue} type={offer.type} />
          <span className="font-mono text-[0.9em] break-all">
            {offer.target ?? "(target not resolved in the file)"}
          </span>
          <span className="text-muted-foreground">
            {resourceLevelLabel(offer.permission)}
          </span>
        </div>
      </div>
    );
  }
  if (row.suggestedType !== null) {
    return (
      <span className="text-muted-foreground">
        Its name looks like a{" "}
        {resourceTypeLabel(catalogue, row.suggestedType).toLowerCase()}.
      </span>
    );
  }
  return (
    <span className="text-muted-foreground">
      Nothing offers it; add one by hand.
    </span>
  );
}

/**
 * What the active release's code asks for (`databricks.yml` and the env
 * entries), and which of it the app already has. An offer is filled into the
 * Add panel, never attached on its own: the person confirms the object and
 * the access mode.
 */
export function RequirementsSection({
  app,
  actor,
  catalogue,
  onAdd,
}: RequirementsSectionProps) {
  const { requirements } = app;
  const rows = requirementRows(app.resources, requirements);
  const summary = requirementsSummary(requirements);
  const gate = manageAvailability(actor);

  function addRequirement(key: string): void {
    const requirement = requirements.items.find((item) => item.key === key);
    if (requirement !== undefined) {
      onAdd(attachDraftFromRequirement(requirement, catalogue));
    }
  }

  const columns: DataTableColumn<RequirementRow>[] = [
    {
      key: "key",
      header: "Key",
      isRowHeader: true,
      cell: (row) => (
        <span className="font-mono text-[0.9em] wrap-break-word">
          {row.key}
        </span>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (row) =>
        row.state === "attached" ? (
          <Lamp state="running" label="Attached" />
        ) : (
          <Lamp
            state="warning"
            label="Missing"
            description="The release refers to it, but no resource of this key is attached."
          />
        ),
    },
    {
      key: "detail",
      header: "Detail",
      cell: (row) => <RequirementDetail row={row} catalogue={catalogue} />,
    },
  ];

  if (gate.isAllowed) {
    columns.push({
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (row) =>
        row.state === "attached" ? null : (
          <Button
            size="small"
            onClick={() => {
              addRequirement(row.key);
            }}
          >
            Add <span className="sr-only">{row.key}</span>
          </Button>
        ),
    });
  }

  return (
    <Section title="Requirements">
      <div className="flex flex-col gap-4">
        <p className="text-muted-foreground">
          {requirements.release_no === null
            ? "No release has been read yet."
            : `Read from release ${requirements.release_no}. `}
          <strong>{summary.text}.</strong>
        </p>
        {gate.isAllowed ? null : (
          <p className="text-sm text-muted-foreground">
            Read only: {gate.reason}
          </p>
        )}
        <DataTable
          caption="Requirements of the active release"
          isCaptionHidden
          columns={columns}
          rows={rows}
          rowKey={(row) => row.key}
          empty={
            <EmptyState title="The release asks for nothing">
              Its env entries and databricks.yml name no resource.
            </EmptyState>
          }
        />
        {requirements.unreferenced_offers.length === 0 ? null : (
          <div className="flex flex-col gap-2">
            <h3>Offered, but nothing refers to it</h3>
            <p className="text-muted-foreground">
              databricks.yml lists these resources, but no env entry uses them.
            </p>
            <ul className="flex flex-col gap-2">
              {requirements.unreferenced_offers.map((offer) => (
                <li
                  key={offer.key}
                  className="flex flex-wrap items-center gap-2"
                >
                  <span className="font-mono text-[0.9em] wrap-break-word">
                    {offer.key}
                  </span>
                  <TypeName catalogue={catalogue} type={offer.type} />
                  <span className="text-muted-foreground">
                    {resourceLevelLabel(offer.permission)}
                  </span>
                  {gate.isAllowed ? (
                    <Button
                      size="small"
                      onClick={() => {
                        onAdd(attachDraftFromOffer(offer, catalogue));
                      }}
                    >
                      Add <span className="sr-only">{offer.key}</span>
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Section>
  );
}
