"use client";

import { useState } from "react";

import { withdrawRequest } from "@/lib/api";
import type { AppDetail, RequestList, RequestRow } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { requestWithdrawnMessage } from "@/lib/app/outcomes";
import {
  RECENT_REQUESTS_SHOWN,
  appChangeRequests,
  canWithdraw,
  limitRequests,
  requestAnswerText,
  requestFormAvailability,
  requestKindLabel,
  requestSummaryLines,
} from "@/lib/app/requests";
import type { AppRequestKind } from "@/lib/app/requests";
import { unavailableReasons } from "@/lib/app/reasons";
import { pollData } from "@/lib/app/view";
import type { PollResult } from "@/lib/poll/schedule";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Loading } from "@/components/loading";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { RequestStatusBadge } from "@/components/status-badge";
import { DelegationRequestForm } from "./delegation-request-form";
import { SettingsRequestForm } from "./settings-request-form";
import type { AppActions } from "./use-app-actions";

export interface RequestsSectionProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  requests: PollResult<RequestList>;
  /** Reads the requests again: offered after a failure. */
  onReloadRequests: () => void;
  /** Reads the app and its requests again: offered after a 409. */
  onReload: () => void;
}

/**
 * The changes an app admin cannot make alone (design 9.4): settings and
 * delegations are asked of a platform admin. The forms are the contract's; the
 * list shows this app's requests with their answers and Withdraw.
 */
export function RequestsSection({
  app,
  actor,
  actions,
  requests,
  onReloadRequests,
  onReload,
}: RequestsSectionProps) {
  const [openForm, setOpenForm] = useState<AppRequestKind | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);

  const requestItems = pollData(requests)?.items ?? null;
  const settings = requestFormAvailability(
    "settings",
    app.slug,
    requestItems,
    actor,
  );
  const delegation = requestFormAvailability(
    "delegation",
    app.slug,
    requestItems,
    actor,
  );
  const reasons = unavailableReasons([
    { label: "Request a change of settings", availability: settings },
    { label: "Request a delegation", availability: delegation },
  ]);

  function toggleForm(kind: AppRequestKind): void {
    setOpenForm((current) => (current === kind ? null : kind));
  }

  async function withdraw(request: RequestRow): Promise<void> {
    setFailure(null);
    const result = await actions.run(
      `withdraw:${request.request_id}`,
      () => withdrawRequest(request.request_id),
      requestWithdrawnMessage(request.kind),
    );
    if (!result.ok) setFailure({ error: result.error });
  }

  const columns: DataTableColumn<RequestRow>[] = [
    {
      key: "request",
      header: "Request",
      isRowHeader: true,
      cell: (request) => (
        <div className="flex flex-col gap-1">
          <div>{requestKindLabel(request.kind)}</div>
          {requestSummaryLines(request).map((line) => (
            <div
              key={line}
              className="font-normal wrap-break-word text-muted-foreground"
            >
              {line}
            </div>
          ))}
          <div className="font-normal wrap-break-word text-muted-foreground">
            Reason: {request.reason}
          </div>
        </div>
      ),
    },
    {
      key: "status",
      header: "State",
      cell: (request) => <RequestStatusBadge status={request.status} />,
    },
    {
      key: "asked",
      header: "Asked",
      cell: (request) => (
        <div className="flex flex-col gap-1">
          <RelativeTime value={request.requested_at} />
          <span className="font-normal wrap-break-word text-muted-foreground">
            by {request.requested_by}
          </span>
        </div>
      ),
    },
    {
      key: "answer",
      header: "Answer",
      cell: (request) => (
        <div className="flex flex-col gap-1">
          <div className="wrap-break-word">{requestAnswerText(request)}</div>
          {request.closed_at === null ? null : (
            <RelativeTime value={request.closed_at} />
          )}
        </div>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (request) => {
        if (!canWithdraw(request, actor?.email ?? null)) return null;
        const state = actions.stateOf(`withdraw:${request.request_id}`);
        return (
          <Button
            size="small"
            isBusy={state === "busy"}
            busyLabel="Withdrawing"
            disabled={state === "blocked"}
            onClick={() => {
              void withdraw(request);
            }}
          >
            Withdraw{" "}
            <span className="sr-only">
              {requestKindLabel(request.kind).toLowerCase()} request
            </span>
          </Button>
        );
      },
    },
  ];

  const view =
    requestItems === null
      ? null
      : limitRequests(
          appChangeRequests(requestItems, app.slug),
          showAll ? Number.POSITIVE_INFINITY : RECENT_REQUESTS_SHOWN,
        );

  return (
    <Section
      title="Requests to a platform admin"
      actions={
        <>
          <Button
            aria-expanded={openForm === "settings"}
            disabled={!settings.isAllowed}
            onClick={() => {
              toggleForm("settings");
            }}
          >
            Request a change of settings
          </Button>
          <Button
            aria-expanded={openForm === "delegation"}
            disabled={!delegation.isAllowed}
            onClick={() => {
              toggleForm("delegation");
            }}
          >
            Request a delegation
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-muted-foreground">
          The name, address, source folder, memory budget and delegations of an
          app are changed by a platform admin. Ask here and follow the answer.
        </p>
        {reasons.length === 0 ? null : (
          <ul className="text-sm text-muted-foreground">
            {reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        )}
        {openForm === "settings" ? (
          <SettingsRequestForm
            app={app}
            actions={actions}
            onClose={() => {
              setOpenForm(null);
            }}
            onReload={onReload}
          />
        ) : null}
        {openForm === "delegation" ? (
          <DelegationRequestForm
            app={app}
            actions={actions}
            onClose={() => {
              setOpenForm(null);
            }}
            onReload={onReload}
          />
        ) : null}
        {failure === null ? null : (
          <ErrorNotice
            error={failure.error}
            context="Could not withdraw the request"
            onReload={onReload}
          />
        )}
        {requests.status === "stale" ? (
          <ErrorNotice
            error={requests.error}
            context="Could not refresh the requests"
            onRetry={onReloadRequests}
          />
        ) : null}
        {requests.status === "loading" ? (
          <Loading label="Loading the requests" />
        ) : null}
        {requests.status === "failed" ? (
          <ErrorNotice
            error={requests.error}
            context="Could not load the requests"
            onRetry={onReloadRequests}
          />
        ) : null}
        {view === null ? null : (
          <>
            <DataTable
              caption="Requests for this app"
              isCaptionHidden
              columns={columns}
              rows={view.shown}
              rowKey={(request) => String(request.request_id)}
              empty={
                <EmptyState title="No requests for this app yet">
                  The requests you send, and the answers, appear here.
                </EmptyState>
              }
            />
            {view.hiddenCount > 0 || showAll ? (
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="small"
                  onClick={() => {
                    setShowAll((current) => !current);
                  }}
                >
                  {showAll
                    ? "Show only the latest"
                    : `Show ${view.hiddenCount} older`}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </div>
    </Section>
  );
}
