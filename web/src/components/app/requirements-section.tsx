"use client";

import type { AppDetail } from "@/lib/api";
import { manageAvailability } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import {
  accessAsLabel,
  attachDraftFromOffer,
  attachDraftFromRequirement,
  checkResultLabel,
  requirementRows,
  requirementsSummary,
  resourceTypeLabel,
} from "@/lib/app/resources";
import type { AttachDraft, RequirementRow } from "@/lib/app/resources";
import { toneMark } from "@/lib/status-presentation";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { Section } from "@/components/section";
import { Badge } from "@/components/ui/badge";

export interface RequirementsSectionProps {
  app: AppDetail;
  actor: Actor | null;
  /** Opens the attach form with this draft filled in. */
  onAttach: (draft: AttachDraft) => void;
}

function RequirementDetail({ row }: { row: RequirementRow }) {
  if (row.resource !== null) {
    const { resource } = row;
    return (
      <div className="flex flex-col gap-1">
        <div className="wrap-break-word">
          {resourceTypeLabel(resource.type)}:{" "}
          <span className="font-mono text-[0.9em]">{resource.target}</span> (
          {resource.permission})
        </div>
        <div className="text-muted-foreground">
          {accessAsLabel(resource.access_as)}
        </div>
        <div className="text-muted-foreground">
          {checkResultLabel(resource.check_result)}
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
        <div className="wrap-break-word">
          {resourceTypeLabel(offer.type)}:{" "}
          <span className="font-mono text-[0.9em]">
            {offer.target ?? "(target not resolved in the file)"}
          </span>{" "}
          ({offer.permission})
        </div>
      </div>
    );
  }
  if (row.suggestedType !== null) {
    return (
      <span className="text-muted-foreground">
        Its name looks like a {resourceTypeLabel(row.suggestedType)}.
      </span>
    );
  }
  return (
    <span className="text-muted-foreground">
      Nothing offers it; attach one by hand.
    </span>
  );
}

/**
 * What the active release's code asks for (`databricks.yml` and the env
 * entries), and which of it the app already has. An offer is filled into the
 * attach form, never attached on its own: the person confirms the target and
 * the identity.
 */
export function RequirementsSection({
  app,
  actor,
  onAttach,
}: RequirementsSectionProps) {
  const { requirements } = app;
  const rows = requirementRows(app.resources, requirements);
  const summary = requirementsSummary(requirements);
  const gate = manageAvailability(actor);

  function attachRequirement(key: string): void {
    const requirement = requirements.items.find((item) => item.key === key);
    if (requirement !== undefined) {
      onAttach(attachDraftFromRequirement(requirement));
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
          <Badge variant="success">
            <span aria-hidden="true">{toneMark("success")}</span>
            Attached
          </Badge>
        ) : (
          <Badge variant="warning">
            <span aria-hidden="true">{toneMark("warning")}</span>
            Missing
          </Badge>
        ),
    },
    {
      key: "detail",
      header: "Detail",
      cell: (row) => <RequirementDetail row={row} />,
    },
    {
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (row) =>
        row.state === "attached" ? null : (
          <Button
            size="small"
            disabled={!gate.isAllowed}
            onClick={() => {
              attachRequirement(row.key);
            }}
          >
            Attach <span className="sr-only">{row.key}</span>
          </Button>
        ),
    },
  ];

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
          <p className="text-sm text-muted-foreground">Attach: {gate.reason}</p>
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
                  <span className="wrap-break-word">
                    <span className="font-mono text-[0.9em]">{offer.key}</span>:{" "}
                    {resourceTypeLabel(offer.type)} ({offer.permission})
                  </span>
                  <Button
                    size="small"
                    disabled={!gate.isAllowed}
                    onClick={() => {
                      onAttach(attachDraftFromOffer(offer));
                    }}
                  >
                    Attach <span className="sr-only">{offer.key}</span>
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Section>
  );
}
