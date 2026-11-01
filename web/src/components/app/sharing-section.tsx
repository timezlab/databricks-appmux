"use client";

import { useState } from "react";

import { deleteGrant, putGrant } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import { manageAvailability } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import { grantActionMessage } from "@/lib/app/outcomes";
import { unavailableReasons } from "@/lib/app/reasons";
import {
  grantLevelChange,
  grantLevelChangePrompt,
  grantRemoveAvailability,
  grantRemovePrompt,
  grantRows,
  permissionLabel,
} from "@/lib/app/sharing";
import type { GrantRow } from "@/lib/app/sharing";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { ErrorNotice } from "@/components/error-notice";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { GrantForm } from "./grant-form";
import type { AppActions } from "./use-app-actions";

export interface SharingSectionProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  onReload: () => void;
}

const TYPE_WORD = { user: "Person", group: "Group", everyone: "Everyone" };

function displayName(row: GrantRow): string {
  return row.type === "everyone" ? "Everyone" : row.name;
}

/** Who may open the app and who manages it. The owner is a fixed first row; a platform admin manages every app and is not listed. */
export function SharingSection({
  app,
  actor,
  actions,
  onReload,
}: SharingSectionProps) {
  const [failure, setFailure] = useState<{
    context: string;
    error: unknown;
  } | null>(null);
  const gate = manageAvailability(actor);
  const rows = grantRows(app);

  async function changeLevel(row: GrantRow): Promise<void> {
    const change = grantLevelChange(row);
    if (change === null) return;
    setFailure(null);
    const result = await actions.run(
      `grant:${row.id}`,
      () => putGrant(app.slug, change.request),
      grantActionMessage("change", displayName(row)),
    );
    if (!result.ok) {
      setFailure({
        context: `Could not change the level of ${displayName(row)}`,
        error: result.error,
      });
    }
  }

  async function remove(row: GrantRow): Promise<void> {
    setFailure(null);
    const result = await actions.run(
      `grant:${row.id}`,
      () => deleteGrant(app.slug, row.type, row.name),
      grantActionMessage("remove", displayName(row)),
    );
    if (!result.ok) {
      setFailure({
        context: `Could not remove ${displayName(row)}`,
        error: result.error,
      });
    }
  }

  const columns: DataTableColumn<GrantRow>[] = [
    {
      key: "name",
      header: "Name",
      isRowHeader: true,
      cell: (row) => (
        <div className="flex flex-col gap-1">
          <span className="wrap-break-word">{displayName(row)}</span>
          {row.isOwner ? (
            <span className="text-muted-foreground">Owner</span>
          ) : null}
        </div>
      ),
    },
    { key: "type", header: "Type", cell: (row) => TYPE_WORD[row.type] },
    {
      key: "level",
      header: "Level",
      cell: (row) => permissionLabel(row.permission),
    },
    {
      key: "granted",
      header: "Granted",
      cell: (row) =>
        row.grantedBy === null ? (
          <span className="text-muted-foreground">Always</span>
        ) : (
          <div className="flex flex-col gap-1">
            <RelativeTime value={row.grantedAt} />
            <span className="wrap-break-word text-muted-foreground">
              by {row.grantedBy}
            </span>
          </div>
        ),
    },
    {
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (row) => {
        const change = grantLevelChange(row);
        const lockOutPrompt = grantLevelChangePrompt(row, actor);
        const removal = grantRemoveAvailability(row, actor);
        const state = actions.stateOf(`grant:${row.id}`);
        const reasons = unavailableReasons([
          { label: "Remove", availability: removal },
        ]);
        return (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              {change === null ? null : lockOutPrompt === null ? (
                <Button
                  size="small"
                  isBusy={state === "busy"}
                  busyLabel="Saving"
                  disabled={!gate.isAllowed || state === "blocked"}
                  onClick={() => {
                    void changeLevel(row);
                  }}
                >
                  {change.label}{" "}
                  <span className="sr-only">{displayName(row)}</span>
                </Button>
              ) : (
                <ConfirmButton
                  size="small"
                  label={change.label}
                  prompt={lockOutPrompt}
                  confirmLabel={`Yes, ${change.label.toLowerCase()}`}
                  isBusy={state === "busy"}
                  busyLabel="Saving"
                  isDisabled={!gate.isAllowed || state === "blocked"}
                  onConfirm={() => {
                    void changeLevel(row);
                  }}
                />
              )}
              <ConfirmButton
                size="small"
                label="Remove"
                prompt={grantRemovePrompt(row, actor?.email ?? "")}
                confirmLabel="Yes, remove"
                isBusy={state === "busy"}
                busyLabel="Removing"
                isDisabled={!removal.isAllowed || state === "blocked"}
                onConfirm={() => {
                  void remove(row);
                }}
              />
            </div>
            {reasons.length === 0 ? null : (
              <ul className="text-sm text-muted-foreground">
                {reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <Section title="Sharing">
      <div className="flex flex-col gap-4">
        {failure === null ? null : (
          <ErrorNotice
            error={failure.error}
            context={failure.context}
            onReload={onReload}
          />
        )}
        <DataTable
          caption="Who can open this app"
          isCaptionHidden
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
        />
        {gate.isAllowed ? null : (
          <p className="text-sm text-muted-foreground">
            Sharing: {gate.reason}
          </p>
        )}
        <GrantForm
          app={app}
          actions={actions}
          isDisabled={!gate.isAllowed}
          onReload={onReload}
        />
      </div>
    </Section>
  );
}
