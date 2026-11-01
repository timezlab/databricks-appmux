"use client";

import { useEffect, useRef, useState } from "react";

import { approveRequest, denyRequest } from "@/lib/api";
import type {
  DecidePermissionRequest,
  PlatformView,
  RequestList,
} from "@/lib/api";
import { pluralize } from "@/lib/format";
import {
  describeDecision,
  inboxItem,
  parseDecision,
} from "@/lib/platform/inbox";
import type {
  Decision,
  DecisionValues,
  InboxChange,
  InboxItem,
} from "@/lib/platform/inbox";
import type { FormErrors } from "@/lib/platform/forms";
import type { PollResult } from "@/lib/poll/schedule";
import { inboxRequests } from "@/lib/request-views";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import { FormActions } from "@/components/form-layout";
import { Loading } from "@/components/loading";
import { Notice } from "@/components/notice";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ActionResult } from "./action-result";
import { FormSummary } from "./form-summary";
import { usePlatformContext } from "./platform-context";
import { useWriteAction } from "./use-write-action";

const EMPTY_DECISION: DecisionValues = { reason: "", note: "" };

/** The id the launcher's "Open the inbox" link points at (`/platform/#inbox`). */
const INBOX_ANCHOR = "inbox";

interface PendingDecision {
  id: number;
  decision: Decision;
}

/**
 * The pending requests, oldest first, each with what it asks next to what is
 * true now, and the two answers. One decision runs at a time: the result stays
 * above the list after the answered card has left it.
 */
export function InboxSection({
  requests,
  view,
}: {
  requests: PollResult<RequestList>;
  view: PlatformView;
}) {
  const { refresh } = usePlatformContext();
  const action = useWriteAction();
  const [pending, setPending] = useState<PendingDecision | null>(null);
  const anchorRef = useRef<HTMLDivElement>(null);

  // The launcher links to #inbox; the section only exists once the data has
  // loaded, so the browser's own scroll to the anchor has nothing to land on.
  useEffect(() => {
    if (window.location.hash === `#${INBOX_ANCHOR}`) {
      anchorRef.current?.scrollIntoView();
    }
  }, []);

  function decide(
    item: InboxItem,
    decision: Decision,
    body: DecidePermissionRequest,
  ): void {
    setPending({ id: item.id, decision });
    void action.run(async () => {
      const row =
        decision === "approve"
          ? await approveRequest(item.id, body)
          : await denyRequest(item.id, body);
      return describeDecision(decision, row);
    });
  }

  return (
    <div id={INBOX_ANCHOR} ref={anchorRef}>
      <Section title="Inbox">
        <div className="flex flex-col gap-4">
          <InboxBody
            requests={requests}
            view={view}
            onRetry={refresh}
            isBusy={action.isBusy}
            pending={pending}
            onDecide={decide}
          />
          <ActionResult
            action={action}
            context="Could not answer the request"
          />
        </div>
      </Section>
    </div>
  );
}

function InboxBody({
  requests,
  view,
  onRetry,
  isBusy,
  pending,
  onDecide,
}: {
  requests: PollResult<RequestList>;
  view: PlatformView;
  onRetry: () => void;
  isBusy: boolean;
  pending: PendingDecision | null;
  onDecide: (
    item: InboxItem,
    decision: Decision,
    body: DecidePermissionRequest,
  ) => void;
}) {
  if (requests.status === "loading") {
    return <Loading label="Loading the inbox" />;
  }
  if (requests.status === "failed") {
    return (
      <ErrorNotice
        error={requests.error}
        context="Could not load the inbox"
        onRetry={onRetry}
      />
    );
  }

  const context = {
    apps: view.apps,
    isSharedSpAllowed: view.settings.shared_sp_delegation_allowed,
    freeMemoryMb: view.memory.free_mb,
  };
  const items = inboxRequests(requests.data.items).map((row) =>
    inboxItem(row, context),
  );

  return (
    <>
      {requests.status === "stale" ? (
        <ErrorNotice
          error={requests.error}
          context="Could not refresh the inbox; showing the last list"
          onRetry={onRetry}
        />
      ) : null}
      {items.length === 0 ? (
        <EmptyState title="No pending requests">
          When someone asks to register an app, to be given a delegation or to
          change a setting, the request waits here for an answer.
        </EmptyState>
      ) : (
        <>
          <p className="text-muted-foreground">
            {pluralize(items.length, "request")} waiting, oldest first. Text in
            a request is written by the person who asked; check it before you
            approve.
          </p>
          {items.map((item) => (
            <InboxCard
              key={item.id}
              item={item}
              isBusy={isBusy}
              busyDecision={
                pending !== null && pending.id === item.id && isBusy
                  ? pending.decision
                  : null
              }
              onDecide={onDecide}
            />
          ))}
        </>
      )}
    </>
  );
}

function ChangeLabel({ change }: { change: InboxChange }) {
  return (
    <>
      <span className="wrap-break-word">{change.label}</span>
      {change.isGate ? (
        <span className="text-muted-foreground"> (gate field)</span>
      ) : null}
      {change.isUnknown ? (
        <span className="text-muted-foreground">
          {" "}
          (not a field this page knows)
        </span>
      ) : null}
    </>
  );
}

function InboxCard({
  item,
  isBusy,
  busyDecision,
  onDecide,
}: {
  item: InboxItem;
  isBusy: boolean;
  busyDecision: Decision | null;
  onDecide: (
    item: InboxItem,
    decision: Decision,
    body: DecidePermissionRequest,
  ) => void;
}) {
  const [values, setValues] = useState<DecisionValues>(EMPTY_DECISION);
  const [errors, setErrors] = useState<FormErrors>({});

  function submit(decision: Decision): void {
    const parsed = parseDecision(item, decision, values);
    if (!parsed.ok) {
      setErrors(parsed.errors);
      return;
    }
    setErrors({});
    onDecide(item, decision, parsed.value);
  }

  return (
    <Card asChild>
      {/* What is asked on the left, the answer on the right where there is room, so three requests do not make a page of forms. */}
      <article className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:gap-6">
        <div className="flex min-w-0 flex-col gap-4">
          <h3 className="wrap-break-word">
            Request #{item.id}: {item.kindLabel}, {item.appText}
          </h3>
          <p className="text-muted-foreground">
            Asked by <span className="wrap-break-word">{item.requestedBy}</span>
            , <RelativeTime value={item.requestedAt} />
          </p>
          <div className="flex flex-col gap-1">
            <p>
              <strong>Their reason</strong>
            </p>
            {item.reason.split("\n").map((line, index) => (
              <p key={`${index}:${line}`} className="wrap-break-word">
                {line}
              </p>
            ))}
          </div>

          <DataTable<InboxChange>
            caption={`What request #${item.id} changes`}
            isCaptionHidden
            rows={item.changes}
            rowKey={(change) => change.field}
            columns={[
              {
                key: "field",
                header: "Field",
                isRowHeader: true,
                cell: (change) => <ChangeLabel change={change} />,
              },
              {
                key: "current",
                header: "Now",
                cell: (change) =>
                  change.current === null ? (
                    <span className="text-muted-foreground">none yet</span>
                  ) : (
                    <span className="wrap-break-word">{change.current}</span>
                  ),
              },
              {
                key: "requested",
                header: "Requested",
                cell: (change) => (
                  <span className="wrap-break-word">{change.requested}</span>
                ),
              },
              {
                key: "note",
                header: "Note",
                cell: (change) => change.note ?? "",
              },
            ]}
          />

          {item.warnings.length === 0 ? null : (
            <Notice tone="warning" title="Before you answer">
              <ul className="list-disc pl-5">
                {item.warnings.map((warning) => (
                  <li key={warning} className="wrap-break-word">
                    {warning}
                  </li>
                ))}
              </ul>
            </Notice>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-4 border-t pt-4 lg:border-t-0 lg:border-l lg:pt-0 lg:pl-6">
          <FormSummary errors={errors} />
          <Field
            label="Reason"
            hint={
              item.requiresReason
                ? "Required to approve this request, because it holds a gate field. Recorded in the audit log."
                : "Optional. Recorded in the audit log."
            }
            error={errors.reason}
          >
            {(control) => (
              <Textarea
                {...control}
                value={values.reason}
                onChange={(event) => {
                  setValues({ ...values, reason: event.target.value });
                }}
              />
            )}
          </Field>
          <Field
            label="Note to the asker"
            hint="Optional. Shown to them with the answer."
            error={errors.note}
          >
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="off"
                value={values.note}
                onChange={(event) => {
                  setValues({ ...values, note: event.target.value });
                }}
              />
            )}
          </Field>
          <FormActions
            primary={
              <Button
                variant="primary"
                isBusy={busyDecision === "approve"}
                busyLabel="Approving"
                disabled={isBusy && busyDecision !== "approve"}
                onClick={() => {
                  submit("approve");
                }}
              >
                Approve
              </Button>
            }
          >
            {/* A denial is final for the asker (they must ask again), so it asks first, as Delete and Detach do. */}
            <ConfirmButton
              label="Deny"
              variant="danger"
              prompt={`Deny request #${item.id}? ${item.requestedBy} is told it was denied.`}
              confirmLabel={`Yes, deny request #${item.id}`}
              isBusy={busyDecision === "deny"}
              busyLabel="Denying"
              isDisabled={isBusy && busyDecision !== "deny"}
              onConfirm={() => {
                submit("deny");
              }}
            />
          </FormActions>
        </div>
      </article>
    </Card>
  );
}
