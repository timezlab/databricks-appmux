"use client";

import { useState } from "react";

import { withdrawRequest } from "@/lib/api";
import type { RequestRow } from "@/lib/api";
import {
  canWithdraw,
  describeRequest,
  olderAnswersToggleLabel,
  reviewerNote,
  visibleOwnRequests,
} from "@/lib/launcher/requests";
import type { OwnRequestsSection } from "@/lib/launcher/requests";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Loading } from "@/components/loading";
import { RelativeTime } from "@/components/relative-time";
import { RequestStatusBadge } from "@/components/status-badge";

export interface OwnRequestsProps {
  section: OwnRequestsSection;
  /** The server withdrew the request; `request` is its row now. */
  onWithdrawn: (request: RequestRow) => void;
  /** Fetches the list again: offered after a failure and after a 409. */
  onReload: () => void;
}

/** The body of the "Your requests" section: the visitor's own requests, newest first, with Withdraw on the pending ones. */
export function OwnRequests({
  section,
  onWithdrawn,
  onReload,
}: OwnRequestsProps) {
  const [showAll, setShowAll] = useState(false);
  const [withdrawingId, setWithdrawingId] = useState<number | null>(null);
  // The rows the server returned for withdrawn requests. "Withdrawn" is final,
  // so they replace the list's older copy at once instead of waiting for the
  // next poll, and Withdraw cannot be pressed twice on the same request.
  const [withdrawn, setWithdrawn] = useState<ReadonlyMap<number, RequestRow>>(
    new Map(),
  );
  const [withdrawFailure, setWithdrawFailure] = useState<{
    error: unknown;
  } | null>(null);

  if (section.status === "loading") {
    return <Loading label="Loading your requests" />;
  }
  if (section.status === "failed") {
    return (
      <ErrorNotice
        error={section.error}
        context="Could not load your requests"
        onReload={onReload}
      />
    );
  }

  const { email } = section;
  const items = section.items.map(
    (request) => withdrawn.get(request.request_id) ?? request,
  );
  const view = visibleOwnRequests(items, email, showAll);
  const olderToggleLabel = olderAnswersToggleLabel(view.hiddenCount, showAll);

  async function withdraw(request: RequestRow): Promise<void> {
    if (withdrawingId !== null) return;
    setWithdrawingId(request.request_id);
    setWithdrawFailure(null);
    try {
      const updated = await withdrawRequest(request.request_id);
      setWithdrawn((current) =>
        new Map(current).set(updated.request_id, updated),
      );
      onWithdrawn(updated);
    } catch (error) {
      // Shown above the table, where the person is looking; nothing else to do.
      setWithdrawFailure({ error });
    } finally {
      setWithdrawingId(null);
    }
  }

  const columns: DataTableColumn<RequestRow>[] = [
    {
      key: "request",
      header: "Request",
      isRowHeader: true,
      cell: (request) => {
        const description = describeRequest(request);
        return (
          <div className="flex flex-col gap-1">
            <div>{description.summary}</div>
            {description.details.map((line) => (
              <div
                key={line}
                className="font-normal text-muted-foreground wrap-break-word"
              >
                {line}
              </div>
            ))}
            <div className="font-normal text-muted-foreground wrap-break-word">
              Reason: {request.reason}
            </div>
          </div>
        );
      },
    },
    {
      key: "status",
      header: "State",
      cell: (request) => <RequestStatusBadge status={request.status} />,
    },
    {
      key: "asked",
      header: "Asked",
      cell: (request) => <RelativeTime value={request.requested_at} />,
    },
    {
      key: "answer",
      header: "Answer",
      cell: (request) => {
        if (request.status === "pending") {
          return (
            <span className="text-muted-foreground">
              Waiting for a platform admin
            </span>
          );
        }
        const note = reviewerNote(request);
        return (
          <div className="flex flex-col gap-1">
            {note === null ? null : (
              <div className="wrap-break-word">{note}</div>
            )}
            <RelativeTime value={request.closed_at} />
          </div>
        );
      },
    },
    {
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (request) =>
        canWithdraw(request, email) ? (
          <Button
            size="small"
            isBusy={withdrawingId === request.request_id}
            busyLabel="Withdrawing"
            onClick={() => {
              void withdraw(request);
            }}
          >
            Withdraw
            <span className="sr-only">
              {" "}
              {describeRequest(request).shortLabel}
            </span>
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      {section.stale === null ? null : (
        <ErrorNotice
          error={section.stale.error}
          context="Could not refresh your requests"
          onRetry={onReload}
        />
      )}
      {withdrawFailure === null ? null : (
        <ErrorNotice
          error={withdrawFailure.error}
          context="Could not withdraw the request"
          onReload={onReload}
        />
      )}
      <DataTable
        caption="Your requests"
        isCaptionHidden
        columns={columns}
        rows={view.rows}
        rowKey={(request) => String(request.request_id)}
        empty={
          <EmptyState title="You have not made any requests">
            Requests for a new app, and the changes you ask for on an app page,
            appear here with their answer.
          </EmptyState>
        }
      />
      {olderToggleLabel === null ? null : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="small"
            onClick={() => {
              setShowAll((current) => !current);
            }}
          >
            {olderToggleLabel}
          </Button>
        </div>
      )}
    </div>
  );
}
