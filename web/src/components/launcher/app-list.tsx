"use client";

import { LoaderCircleIcon } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { setOpenMode } from "@/lib/api";
import type { AppList, OpenMode } from "@/lib/api";
import {
  launcherAppRows,
  shouldExplainOpenModes,
  shouldOfferAppFilter,
  visibleAppRows,
} from "@/lib/launcher/apps";
import type { LauncherAppRow } from "@/lib/launcher/apps";
import {
  OPEN_MODE_EXPLANATION,
  OPEN_MODE_LABEL,
  openAppWithMode,
  openAsUserPrompt,
  openModeNeedsConfirmation,
} from "@/lib/launcher/open";
import type { OpenDependencies } from "@/lib/launcher/open";
import { appsEmptyCopy } from "@/lib/launcher/page";
import { pluralize } from "@/lib/format";
import type { PollResult } from "@/lib/poll/schedule";
import { AppLink } from "@/components/app-link";
import { Button, uiButtonLook } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import { LiveRegion } from "@/components/live-region";
import { RouteStrip } from "@/components/route-strip";
import { SkeletonTable } from "@/components/skeletons";
import { AppStateBadge } from "@/components/status-badge";
import { Button as UiButton } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const OPEN_DEPENDENCIES: OpenDependencies = {
  setMode: setOpenMode,
  navigate: (href) => {
    window.location.assign(href);
  },
};

interface OpeningApp {
  slug: string;
  mode: OpenMode;
}

/** Where a platform admin registers an app: the Platform's Apps page. */
const PLATFORM_REGISTER_HREF = "/platform/apps/";

export interface AppsSectionProps {
  apps: PollResult<AppList>;
  /** `null` while `GET /me` has not answered. */
  isPlatformAdmin: boolean | null;
  /** Fetches the list again: offered after a failure. */
  onReload: () => void;
  /** Opens the launcher's request form: the next step for a person with no app. */
  onRequestApp: () => void;
}

/** The list's place while it loads: the words and spinner, announced, over rows of the table's shape. */
function AppsLoading() {
  return (
    <div className="flex flex-col gap-3">
      <div
        className="flex items-center gap-2 text-sm text-muted-foreground"
        role="status"
      >
        <LoaderCircleIcon
          className="size-4 shrink-0 animate-spinner"
          aria-hidden="true"
        />
        <span>Loading apps…</span>
      </div>
      <SkeletonTable rows={4} columns={4} />
    </div>
  );
}

/**
 * The apps the visitor may open, as one compact table: the name with its
 * route under it, the state, the person's level, and the way to open the app;
 * what needs attention comes first. A search box appears once the list is
 * long enough to need one; a "What is the difference?" disclosure appears
 * only for a person who is offered both ways to open an app.
 */
export function AppsSection({
  apps,
  isPlatformAdmin,
  onReload,
  onRequestApp,
}: AppsSectionProps) {
  const [query, setQuery] = useState("");
  const [opening, setOpening] = useState<OpeningApp | null>(null);
  const [openFailure, setOpenFailure] = useState<{
    appName: string;
    error: unknown;
  } | null>(null);

  if (apps.status === "loading") return <AppsLoading />;
  if (apps.status === "failed") {
    return (
      <ErrorNotice
        error={apps.error}
        context="Could not load your apps"
        onReload={onReload}
      />
    );
  }

  const rows = launcherAppRows(apps.data.items);
  const shownRows = visibleAppRows(rows, query);
  const isSearching = shouldOfferAppFilter(rows.length);
  const hasQuery = isSearching && query.trim() !== "";

  async function open(row: LauncherAppRow, mode: OpenMode): Promise<void> {
    if (opening !== null) return;
    setOpening({ slug: row.slug, mode });
    setOpenFailure(null);
    const result = await openAppWithMode(row.slug, mode, OPEN_DEPENDENCIES);
    // On success the page is already leaving; clearing is for the Back button.
    setOpening(null);
    if (result.status === "failed") {
      setOpenFailure({ appName: row.displayName, error: result.error });
    }
  }

  const columns: DataTableColumn<LauncherAppRow>[] = [
    {
      key: "name",
      header: "Name",
      isRowHeader: true,
      cell: (row) => (
        <div className="flex min-w-0 flex-col items-start gap-1">
          <span className="wrap-break-word">{row.displayName}</span>
          <RouteStrip slug={row.slug} />
        </div>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (row) => <AppStateBadge state={row.state} />,
    },
    {
      key: "level",
      header: "Your level",
      cell: (row) => (
        <span className="whitespace-nowrap">{row.levelLabel}</span>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (row) => (
        <RowActions
          row={row}
          opening={opening}
          onOpen={(mode) => {
            void open(row, mode);
          }}
        />
      ),
    },
  ];

  const copy = appsEmptyCopy(isPlatformAdmin);
  // While it is not known who the person is, no next step is offered rather than the wrong one.
  const nextStep =
    isPlatformAdmin === true ? (
      <UiButton asChild {...uiButtonLook("primary", "small")}>
        <Link href={PLATFORM_REGISTER_HREF}>Register an app</Link>
      </UiButton>
    ) : isPlatformAdmin === false ? (
      <Button variant="primary" size="small" onClick={onRequestApp}>
        Request an app
      </Button>
    ) : undefined;
  const emptyState =
    rows.length === 0 ? (
      <EmptyState title={copy.title} action={nextStep}>
        {copy.body}
      </EmptyState>
    ) : (
      <EmptyState
        title="No app matches the search"
        action={
          <Button
            size="small"
            onClick={() => {
              setQuery("");
            }}
          >
            Clear the search
          </Button>
        }
      />
    );

  return (
    <div className="flex flex-col gap-4">
      {apps.status === "stale" ? (
        <ErrorNotice
          error={apps.error}
          context="Could not refresh your apps"
          onRetry={onReload}
        />
      ) : null}
      {openFailure === null ? null : (
        <ErrorNotice
          error={openFailure.error}
          context={`Could not open ${openFailure.appName}`}
          onReload={onReload}
        />
      )}
      {isSearching ? (
        <div className="w-full sm:max-w-sm">
          <Field
            label="Search apps"
            hint={
              hasQuery
                ? `${shownRows.length} of ${pluralize(rows.length, "app")} match.`
                : "Matches the name or the slug."
            }
          >
            {(control) => (
              <Input
                {...control}
                type="search"
                value={query}
                autoComplete="off"
                onChange={(event) => {
                  setQuery(event.target.value);
                }}
              />
            )}
          </Field>
          <LiveRegion
            message={
              hasQuery ? `${pluralize(shownRows.length, "app")} found` : null
            }
          />
        </div>
      ) : null}
      {shouldExplainOpenModes(rows) ? <OpenModeDisclosure /> : null}
      <DataTable
        caption="Apps you can open"
        isCaptionHidden
        columns={columns}
        rows={shownRows}
        rowKey={(row) => row.slug}
        empty={emptyState}
      />
    </div>
  );
}

/** Small and closed: most people never need it, and the ones who do find it beside the buttons it explains. */
function OpenModeDisclosure() {
  return (
    <details className="text-sm">
      <summary className="w-fit cursor-pointer font-medium">
        What is the difference?
      </summary>
      <div className="mt-2 flex max-w-prose flex-col gap-2 text-muted-foreground">
        <p>{OPEN_MODE_EXPLANATION.intro}</p>
        <ul className="list-disc pl-5">
          <li>
            <strong>{OPEN_MODE_LABEL.without_token}.</strong>{" "}
            {OPEN_MODE_EXPLANATION.withoutToken}
          </li>
          <li>
            <strong>{OPEN_MODE_LABEL.as_user}.</strong>{" "}
            {OPEN_MODE_EXPLANATION.asUser}
          </li>
        </ul>
        <p>{OPEN_MODE_EXPLANATION.remembered}</p>
      </div>
    </details>
  );
}

interface RowActionsProps {
  row: LauncherAppRow;
  opening: OpeningApp | null;
  onOpen: (mode: OpenMode) => void;
}

/**
 * What a person can do with one app: Manage (only if they manage it), then the
 * way to open it, with the main one last. An app that cannot be opened says why
 * in the place Open would be.
 */
function RowActions({ row, opening, onOpen }: RowActionsProps) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 wrap-break-word">
      {row.manageHref === null ? null : (
        <Link href={row.manageHref}>
          Manage
          <span className="sr-only"> {row.displayName}</span>
        </Link>
      )}
      <OpenControls row={row} opening={opening} onOpen={onOpen} />
    </div>
  );
}

function OpenControls({ row, opening, onOpen }: RowActionsProps) {
  const { open } = row;
  switch (open.kind) {
    case "unavailable":
      return <span className="text-muted-foreground">{open.reason}</span>;
    case "open":
      return (
        <AppLink slug={row.slug} buttonVariant="primary" size="small">
          Open
          <span className="sr-only"> {row.displayName}</span>
        </AppLink>
      );
    case "choose-mode": {
      const isOpening = (mode: OpenMode): boolean =>
        opening?.slug === row.slug && opening.mode === mode;
      // `modes` lists the default (without token) first; the main button goes last.
      return (
        <div className="flex flex-wrap items-center gap-2">
          {open.modes.toReversed().map((mode) =>
            openModeNeedsConfirmation(mode) ? (
              <ConfirmButton
                key={mode}
                label={OPEN_MODE_LABEL[mode]}
                prompt={openAsUserPrompt(row.displayName)}
                confirmLabel="Yes, open as a user"
                confirmVariant="primary"
                size="small"
                isBusy={isOpening(mode)}
                busyLabel="Opening"
                onConfirm={() => {
                  onOpen(mode);
                }}
              />
            ) : (
              <Button
                key={mode}
                variant="primary"
                size="small"
                isBusy={isOpening(mode)}
                busyLabel="Opening"
                onClick={() => {
                  onOpen(mode);
                }}
              >
                {OPEN_MODE_LABEL[mode]}
                <span className="sr-only"> {row.displayName}</span>
              </Button>
            ),
          )}
        </div>
      );
    }
  }
}
