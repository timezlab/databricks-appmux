"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";

import { deleteGrant, putGrant } from "@/lib/api";
import type { AppDetail, AppPermission, RequestRow } from "@/lib/api";
import { manageAvailability } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import { grantActionMessage } from "@/lib/app/outcomes";
import { appPagePath } from "@/lib/app/params";
import {
  delegationChoices,
  identityModes,
  pendingRequestOfKind,
  requestFormAvailability,
} from "@/lib/app/requests";
import type { IdentityMode } from "@/lib/app/requests";
import {
  PRINCIPAL_TYPE_LABEL,
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
import { Lamp } from "@/components/lamp";
import type { LampState } from "@/components/lamp";
import { PrincipalIcon } from "@/components/principal-combobox";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DelegationRequestForm } from "./delegation-request-form";
import { GrantForm } from "./grant-form";
import type { AppActions } from "./use-app-actions";

const LEVELS: readonly AppPermission[] = ["CAN_USE", "CAN_MANAGE"];

function isPermission(value: string): value is AppPermission {
  return LEVELS.some((level) => level === value);
}

function displayName(row: GrantRow): string {
  return row.type === "everyone" ? "Everyone" : row.name;
}

// ------------------------------------------------------------- permissions

/**
 * The question before an app admin lowers their own grant, which may take
 * this page away from them: inline in the row, focus on Cancel, Escape
 * cancels.
 */
function LowerOwnAccessQuestion({
  prompt,
  onCancel,
  onConfirm,
}: {
  prompt: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const promptId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);
  return (
    <span
      role="group"
      aria-labelledby={promptId}
      className="inline-flex flex-col gap-2 rounded-md border-2 border-tone-danger-border bg-tone-danger-bg px-3 py-2 text-tone-danger-text"
      onKeyDown={(event) => {
        if (event.key === "Escape") onCancel();
      }}
    >
      <span id={promptId} className="font-semibold">
        {prompt}
      </span>
      <span className="flex flex-wrap gap-2">
        <Button ref={cancelRef} size="small" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="small" variant="danger-filled" onClick={onConfirm}>
          Yes, lower my access
        </Button>
      </span>
    </span>
  );
}

export interface PermissionsSectionProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  onReload: () => void;
}

interface LoweringQuestion {
  rowId: string;
  prompt: string;
}

/**
 * Who may open the app and who manages it, as one list: the owner first (a
 * fixed row), then the grants, each level changed in place and each grant
 * removed after a question. A platform admin manages every app and is not
 * listed. Someone who cannot manage the app sees the list only.
 */
export function PermissionsSection({
  app,
  actor,
  actions,
  onReload,
}: PermissionsSectionProps) {
  const [failure, setFailure] = useState<{
    context: string;
    error: unknown;
  } | null>(null);
  // The level being saved, shown in the select while the call runs.
  const [changing, setChanging] = useState<{
    rowId: string;
    permission: AppPermission;
  } | null>(null);
  const [lowering, setLowering] = useState<LoweringQuestion | null>(null);
  const baseId = useId();
  const gate = manageAvailability(actor);
  const rows = grantRows(app);

  async function changeLevel(row: GrantRow): Promise<void> {
    const change = grantLevelChange(row);
    if (change === null) return;
    setFailure(null);
    setLowering(null);
    setChanging({ rowId: row.id, permission: change.request.permission });
    const result = await actions.run(
      `grant:${row.id}`,
      () => putGrant(app.slug, change.request),
      grantActionMessage("change", displayName(row)),
    );
    setChanging(null);
    if (!result.ok) {
      setFailure({
        context: `Could not change the level of ${displayName(row)}`,
        error: result.error,
      });
    }
  }

  function pickLevel(row: GrantRow, permission: AppPermission): void {
    if (permission === row.permission) return;
    const prompt = grantLevelChangePrompt(row, actor);
    if (prompt === null) {
      void changeLevel(row);
    } else {
      setLowering({ rowId: row.id, prompt });
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

  function levelCell(row: GrantRow) {
    const state = actions.stateOf(`grant:${row.id}`);
    const canChange =
      gate.isAllowed && grantLevelChange(row) !== null && !row.isOwner;
    if (!canChange) {
      return (
        <span className="flex flex-col gap-1">
          <span>{permissionLabel(row.permission)}</span>
          {row.isOwner ? (
            <span className="text-sm text-muted-foreground">
              Always, as the owner
            </span>
          ) : null}
        </span>
      );
    }
    const triggerId = `${baseId}-level-${row.id}`;
    if (lowering?.rowId === row.id) {
      return (
        <LowerOwnAccessQuestion
          prompt={lowering.prompt}
          onCancel={() => {
            setLowering(null);
            // The select comes back in the question's place; the keyboard goes back to it.
            requestAnimationFrame(() => {
              document.getElementById(triggerId)?.focus();
            });
          }}
          onConfirm={() => {
            void changeLevel(row);
          }}
        />
      );
    }
    const shown =
      changing?.rowId === row.id ? changing.permission : row.permission;
    return (
      <span className="flex flex-wrap items-center gap-2">
        <Select
          value={shown}
          disabled={state !== "idle"}
          onValueChange={(value) => {
            if (isPermission(value)) pickLevel(row, value);
          }}
        >
          <SelectTrigger
            id={triggerId}
            size="sm"
            aria-label={`Level of ${displayName(row)}`}
            className="w-36"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent label={`Level of ${displayName(row)}`}>
            {LEVELS.map((level) => (
              <SelectItem key={level} value={level}>
                {permissionLabel(level)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {state === "busy" ? (
          <span className="text-sm text-muted-foreground" role="status">
            Saving…
          </span>
        ) : null}
      </span>
    );
  }

  const columns: DataTableColumn<GrantRow>[] = [
    {
      key: "who",
      header: "Who",
      isRowHeader: true,
      cell: (row) => (
        <span className="flex min-w-0 items-start gap-2">
          <PrincipalIcon type={row.type} className="mt-0.5" />
          <span className="flex min-w-0 flex-col gap-0.5">
            <span
              className={
                row.type === "service_principal"
                  ? "font-mono text-[0.9em] break-all"
                  : "wrap-break-word"
              }
            >
              {displayName(row)}
            </span>
            <span className="text-sm font-normal text-muted-foreground">
              {row.isOwner ? "Owner" : PRINCIPAL_TYPE_LABEL[row.type]}
            </span>
          </span>
        </span>
      ),
    },
    { key: "level", header: "Level", cell: levelCell },
    {
      key: "granted",
      header: "Granted",
      cell: (row) =>
        row.grantedBy === null ? (
          <span className="text-muted-foreground">Always</span>
        ) : (
          <span className="flex flex-col gap-1">
            <RelativeTime value={row.grantedAt} />
            <span className="wrap-break-word text-muted-foreground">
              by {row.grantedBy}
            </span>
          </span>
        ),
    },
  ];
  if (gate.isAllowed) {
    columns.push({
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (row) => {
        const removal = grantRemoveAvailability(row, actor);
        if (!removal.isAllowed) {
          return (
            <span className="text-sm text-muted-foreground">
              {removal.reason}
            </span>
          );
        }
        const state = actions.stateOf(`grant:${row.id}`);
        // The row's key is also busy while its level changes; Remove waits then.
        const isRemoving = state === "busy" && changing?.rowId !== row.id;
        return (
          <ConfirmButton
            size="small"
            variant="danger"
            label="Remove"
            prompt={grantRemovePrompt(row, actor?.email ?? "")}
            confirmLabel="Yes, remove"
            isBusy={isRemoving}
            busyLabel="Removing"
            isDisabled={state !== "idle" && !isRemoving}
            onConfirm={() => {
              void remove(row);
            }}
          />
        );
      },
    });
  }

  return (
    <Section title="Permissions">
      <div className="flex flex-col gap-4">
        <p className="text-muted-foreground">
          Who can open the app (Can use) and who can manage it (Can manage). A
          platform admin manages every app and is not listed.
        </p>
        {failure === null ? null : (
          <ErrorNotice
            error={failure.error}
            context={failure.context}
            onReload={onReload}
          />
        )}
        <DataTable
          caption="Who can open or manage this app"
          isCaptionHidden
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
        />
        {gate.isAllowed ? (
          <GrantForm app={app} actions={actions} onReload={onReload} />
        ) : (
          <p className="text-sm text-muted-foreground">
            Read only: {gate.reason}
          </p>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------- identity

function identityLamp(mode: IdentityMode): LampState {
  if (mode.isInEffect) return "running";
  return mode.isSwitchedOff ? "stopped" : "neutral";
}

export interface IdentitySectionProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  /** The requests as last read; `null` while they load. */
  requests: readonly RequestRow[] | null;
  onReload: () => void;
}

/**
 * How the app reaches Databricks: as the visitor (OBO), as the shared service
 * principal, or as its own service principal; which is in effect; and the
 * request that asks a platform admin for a delegation.
 */
export function IdentitySection({
  app,
  actor,
  actions,
  requests,
  onReload,
}: IdentitySectionProps) {
  const [isFormOpen, setIsFormOpen] = useState(false);
  const openerRef = useRef<HTMLButtonElement>(null);
  const modes = identityModes(app);
  const choices = delegationChoices(app);
  const pending =
    requests === null
      ? null
      : pendingRequestOfKind(requests, app.slug, "delegation");
  const hasSomethingToAsk =
    choices.obo.state === "available" || choices.sharedSp.state === "available";
  const formAvailability = requestFormAvailability(
    "delegation",
    app.slug,
    requests,
    actor,
  );
  const availability = !hasSomethingToAsk
    ? {
        isAllowed: false,
        reason:
          "Nothing left to ask for: each delegation is in effect or switched off by the platform.",
      }
    : formAvailability;
  const requestsPath = appPagePath(app.slug, "requests");

  return (
    <Section
      title="Identity"
      actions={
        <Button
          ref={openerRef}
          aria-expanded={isFormOpen}
          disabled={!availability.isAllowed}
          onClick={() => {
            setIsFormOpen((current) => !current);
          }}
        >
          Request a delegation
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-muted-foreground">
          The identity the app reaches Databricks with. A platform admin
          delegates OBO or the shared service principal to one app; the
          app&apos;s own service principal needs no delegation.
        </p>
        {availability.isAllowed ? null : (
          <p className="text-sm text-muted-foreground">
            Request a delegation: {availability.reason}
          </p>
        )}
        {pending === null ? null : (
          <p className="flex flex-wrap items-center gap-2 text-sm">
            <Lamp state="warning" label="Requested" />
            <span>A delegation request is waiting for a platform admin.</span>
            {requestsPath === null ? null : (
              <Link href={requestsPath}>See it under Requests</Link>
            )}
          </p>
        )}
        <ul className="flex flex-col divide-y rounded-lg border bg-card">
          {modes.map((mode) => (
            <li key={mode.id} className="flex flex-col gap-1 p-4">
              <span className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
                <span className="font-semibold">{mode.label}</span>
                <Lamp state={identityLamp(mode)} label={mode.stateText} />
              </span>
              <span className="text-sm text-muted-foreground">
                {mode.description}
              </span>
              {mode.howToGet === null ? null : (
                <span className="text-sm">{mode.howToGet}</span>
              )}
            </li>
          ))}
        </ul>
        {isFormOpen ? (
          <DelegationRequestForm
            app={app}
            actions={actions}
            onClose={() => {
              setIsFormOpen(false);
              openerRef.current?.focus();
            }}
            onReload={onReload}
          />
        ) : null}
      </div>
    </Section>
  );
}
