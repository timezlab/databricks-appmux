"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { grantDelegation, revokeDelegation } from "@/lib/api";
import type { DelegationKind, PlatformApp, PlatformView } from "@/lib/api";
import {
  DELEGATION_INFO,
  delegationActionProblem,
  delegationCell,
  parseDelegationReason,
} from "@/lib/platform/delegations";
import type { DelegationAction } from "@/lib/platform/delegations";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { Field } from "@/components/field";
import { FORM_STACK_CLASSES, FormActions } from "@/components/form-layout";
import { Notice } from "@/components/notice";
import { Section } from "@/components/section";
import { Textarea } from "@/components/ui/textarea";
import { ActionResult } from "./action-result";
import { FormSummary } from "./form-summary";
import { useWriteAction } from "./use-write-action";
import type { WriteAction } from "./use-write-action";

const KINDS: readonly DelegationKind[] = ["obo", "shared_sp"];

interface Selection {
  slug: string;
  kind: DelegationKind;
  action: DelegationAction;
}

/**
 * Which apps may receive the visitor's token (OBO) or the appmux service
 * principal's token (shared SP). A grant or a revoke asks for a reason, which
 * the audit log keeps.
 */
export function DelegationsSection({ view }: { view: PlatformView }) {
  const [selection, setSelection] = useState<Selection | null>(null);
  // Held here, not in the form: the form closes on success and the result must stay.
  const action = useWriteAction();
  const isSharedSpAllowed = view.settings.shared_sp_delegation_allowed;
  const selectedApp =
    selection === null
      ? null
      : (view.apps.find((app) => app.slug === selection.slug) ?? null);

  return (
    <Section title="Delegations">
      <div className="flex flex-col gap-4">
        <p className="text-muted-foreground">
          A delegation lets an app receive a token it could not otherwise have.
          Granting the shared service principal is possible only while the
          shared_sp_delegation_allowed setting is on
          {isSharedSpAllowed ? " (it is on now)" : " (it is off now)"}; revoking
          is always possible.
        </p>
        <DataTable<PlatformApp>
          caption="Delegations of each app"
          isCaptionHidden
          rows={view.apps}
          rowKey={(app) => app.slug}
          empty={<EmptyState title="No apps registered" />}
          columns={[
            {
              key: "slug",
              header: "App",
              isRowHeader: true,
              cell: (app) => (
                <span className="font-mono text-[0.9em] wrap-break-word">
                  {app.slug}
                </span>
              ),
            },
            ...KINDS.map((kind) => ({
              key: kind,
              header: DELEGATION_INFO[kind].label,
              cell: (app: PlatformApp) => {
                const cell = delegationCell(app, kind, isSharedSpAllowed);
                const rowAction = cell.action;
                const verb = rowAction === "grant" ? "Grant" : "Revoke";
                return (
                  <span className="flex flex-wrap items-center gap-2">
                    <strong>{cell.stateText}</strong>
                    {rowAction === null ? null : (
                      <Button
                        size="small"
                        variant={
                          cell.action === "revoke" ? "danger" : "secondary"
                        }
                        aria-label={`${verb} ${DELEGATION_INFO[kind].label} for ${app.slug}`}
                        onClick={() => {
                          setSelection({
                            slug: app.slug,
                            kind,
                            action: rowAction,
                          });
                        }}
                      >
                        {verb}
                      </Button>
                    )}
                    {cell.blockedReason === null ? null : (
                      // The reason names a setting key, one long word, that must still wrap in a phone's stacked row.
                      <span className="min-w-0 text-muted-foreground wrap-anywhere">
                        {cell.blockedReason}
                      </span>
                    )}
                  </span>
                );
              },
            })),
          ]}
        />
        {selection !== null && selectedApp === null ? (
          <Notice tone="warning" title="That app is no longer in the list">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="small"
                onClick={() => {
                  setSelection(null);
                }}
              >
                Close
              </Button>
            </div>
          </Notice>
        ) : null}
        {selection !== null && selectedApp !== null ? (
          <ReasonForm
            key={`${selection.slug}:${selection.kind}:${selection.action}`}
            app={selectedApp}
            action={action}
            selection={selection}
            isSharedSpAllowed={isSharedSpAllowed}
            onClose={() => {
              setSelection(null);
            }}
          />
        ) : null}
        <ActionResult
          action={action}
          context="Could not change the delegation"
        />
      </div>
    </Section>
  );
}

function ReasonForm({
  app,
  action,
  selection,
  isSharedSpAllowed,
  onClose,
}: {
  app: PlatformApp;
  action: WriteAction;
  selection: Selection;
  isSharedSpAllowed: boolean;
  onClose: () => void;
}) {
  const { kind, action: verb } = selection;
  const info = DELEGATION_INFO[kind];
  const [reason, setReason] = useState("");
  const [hasTried, setHasTried] = useState(false);

  // The app or the master switch may have moved since the button was pressed.
  const problem = delegationActionProblem(app, kind, verb, isSharedSpAllowed);
  const parsed = parseDelegationReason(app, reason);
  const errors = hasTried && !parsed.ok ? parsed.errors : {};
  const title = `${verb === "grant" ? "Grant" : "Revoke"} ${info.label} for ${app.slug}`;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setHasTried(true);
    if (problem !== null || !parsed.ok) return;
    const body = parsed.value;
    void action.run(async () => {
      if (verb === "grant") {
        await grantDelegation(app.slug, kind, body);
      } else {
        await revokeDelegation(app.slug, kind, body);
      }
      onClose();
      return {
        tone: "success",
        title:
          verb === "grant"
            ? `Allowed ${info.label} for ${app.slug}`
            : `Revoked ${info.label} for ${app.slug}`,
        detail: verb === "grant" ? info.grantEffect : info.revokeEffect,
      };
    });
  }

  return (
    <form
      className={`${FORM_STACK_CLASSES} max-w-140 border-t pt-4`}
      onSubmit={handleSubmit}
    >
      <h3 className="wrap-break-word">{title}</h3>
      <p>
        <strong>The app receives:</strong> {info.receives}
      </p>
      <Notice
        tone={verb === "grant" && kind === "shared_sp" ? "warning" : "info"}
        title={verb === "grant" ? "If you grant this" : "If you revoke this"}
      >
        <p>{verb === "grant" ? info.grantEffect : info.revokeEffect}</p>
      </Notice>
      {problem === null ? null : <Notice tone="danger" title={problem} />}
      <FormSummary errors={errors} />
      <Field
        label="Reason"
        hint="Why, in a sentence. Recorded in the audit log."
        isRequired
        error={errors.reason}
      >
        {(control) => (
          <Textarea
            {...control}
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
            }}
          />
        )}
      </Field>
      <FormActions
        primary={
          <Button
            type="submit"
            variant={verb === "revoke" ? "danger-filled" : "primary"}
            isBusy={action.isBusy}
            busyLabel={verb === "grant" ? "Granting" : "Revoking"}
            disabled={problem !== null}
          >
            {verb === "grant" ? "Grant" : "Revoke"}
          </Button>
        }
      >
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
      </FormActions>
    </form>
  );
}
