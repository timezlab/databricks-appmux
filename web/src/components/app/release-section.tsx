"use client";

import { LockIcon } from "lucide-react";
import { useRef, useState } from "react";

import {
  activateRelease,
  deleteRelease,
  lockRelease,
  unlockRelease,
} from "@/lib/api";
import type { AppDetail, ReleaseSummary } from "@/lib/api";
import { appActionAvailability } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import { releaseActionMessage } from "@/lib/app/outcomes";
import { unavailableReasons } from "@/lib/app/reasons";
import type { ReleaseMenuAction } from "@/lib/app/releases";
import {
  reconcileSelection,
  releaseLockLabel,
  releaseMarkers,
  releasesNewestFirst,
} from "@/lib/app/status";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { ReleaseStatusBadge } from "@/components/status-badge";
import { UpdateButton, useAppControls } from "./app-controls";
import { ReleaseRowMenu } from "./release-actions";
import type { ReleaseWrite } from "./release-actions";
import { ReleaseDeleteConfirm } from "./release-delete-confirm";
import { ReleaseEditForm } from "./release-edit-form";
import { ReleaseFailure } from "./release-failure";
import { SourceViewerPanel } from "./source-viewer-panel";
import type { AppActions } from "./use-app-actions";
import { useSourceViewer } from "./use-source-viewer";

export interface ReleaseSectionProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  onReload: () => void;
}

function writeCall(
  action: ReleaseWrite,
  slug: string,
  releaseNo: number,
): Promise<unknown> {
  switch (action) {
    case "activate":
      return activateRelease(slug, releaseNo);
    case "lock":
      return lockRelease(slug, releaseNo);
    case "unlock":
      return unlockRelease(slug, releaseNo);
    case "delete":
      return deleteRelease(slug, releaseNo);
  }
}

/** Which release's name and tags are open for editing, and the field the person chose. */
interface EditTarget {
  releaseNo: number;
  field: "name" | "tags";
}

/** The number, the name, the target mark and the lock, as the row's header. */
function ReleaseName({ release }: { release: ReleaseSummary }) {
  const markers = releaseMarkers(release);
  const lock = releaseLockLabel(release);
  return (
    <div className="flex flex-col gap-1">
      <div>
        <span className="whitespace-nowrap">#{release.release_no}</span>
        {release.name === null ? null : (
          <span className="wrap-break-word">, {release.name}</span>
        )}
      </div>
      {markers.length === 0 ? null : (
        <div className="text-sm text-muted-foreground">
          {markers.join(", ")}
        </div>
      )}
      {lock === null ? null : (
        <div className="inline-flex items-center gap-1 text-sm text-muted-foreground">
          <LockIcon className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="wrap-break-word">{lock}</span>
        </div>
      )}
    </div>
  );
}

/**
 * The Deployments section: the releases with a row menu each, Update, and the
 * panels a menu choice opens under the table (edit, delete question, source).
 * A failed release explains itself on its row; the one the app was moving to
 * opens with its output.
 */
export function ReleaseSection({
  app,
  actor,
  actions,
  onReload,
}: ReleaseSectionProps) {
  const [editing, setEditing] = useState<EditTarget | null>(null);
  const [deletingNo, setDeletingNo] = useState<number | null>(null);
  const [failure, setFailure] = useState<{
    context: string;
    error: unknown;
  } | null>(null);
  const triggers = useRef(new Map<number, HTMLButtonElement>());
  const controls = useAppControls(app, actions);
  const releases = releasesNewestFirst(app.releases);
  const releaseNos = app.releases.map((release) => release.release_no);
  const source = useSourceViewer(app.slug, releaseNos);
  const update = appActionAvailability("update", app, actor);
  const updateReasons = unavailableReasons([
    { label: "Update", availability: update },
  ]);

  // A poll can delete the release a panel is open on; the panel goes with it.
  const openEditNo = reconcileSelection(
    editing?.releaseNo ?? null,
    app.releases,
  );
  const editedRelease =
    app.releases.find((release) => release.release_no === openEditNo) ?? null;
  const openDeleteNo = reconcileSelection(deletingNo, app.releases);

  /** Puts the keyboard back on the menu a panel was opened from. */
  function focusTrigger(releaseNo: number | null): void {
    if (releaseNo === null) return;
    triggers.current.get(releaseNo)?.focus();
  }

  async function write(
    action: ReleaseWrite,
    release: ReleaseSummary,
  ): Promise<void> {
    setFailure(null);
    const releaseNo = release.release_no;
    const result = await actions.run(
      `${action}:${String(releaseNo)}`,
      () => writeCall(action, app.slug, releaseNo),
      releaseActionMessage(action, releaseNo),
    );
    if (!result.ok) {
      setFailure({
        context: `Could not ${action} release ${String(releaseNo)}`,
        error: result.error,
      });
    }
  }

  function choose(action: ReleaseMenuAction, release: ReleaseSummary): void {
    const releaseNo = release.release_no;
    switch (action) {
      case "activate":
      case "lock":
      case "unlock":
        void write(action, release);
        return;
      case "rename":
        setEditing({ releaseNo, field: "name" });
        return;
      case "tag":
        setEditing({ releaseNo, field: "tags" });
        return;
      case "delete":
        setDeletingNo(releaseNo);
        return;
      case "view-source":
        if (source.state.releaseNo === releaseNo) {
          source.close();
          focusTrigger(releaseNo);
        } else {
          source.open(releaseNo);
        }
        return;
    }
  }

  const columns: DataTableColumn<ReleaseSummary>[] = [
    {
      key: "release",
      header: "Release",
      isRowHeader: true,
      cell: (release) => <ReleaseName release={release} />,
    },
    {
      key: "state",
      header: "State",
      cell: (release) => (
        <div className="flex min-w-0 flex-col gap-1">
          <div>
            <ReleaseStatusBadge status={release.status} />
          </div>
          {release.status === "failed" ? (
            <ReleaseFailure slug={app.slug} release={release} />
          ) : release.status_reason === null ? null : (
            <div className="text-sm text-muted-foreground wrap-break-word">
              {release.status_reason}
            </div>
          )}
        </div>
      ),
    },
    {
      key: "tags",
      header: "Tags",
      cell: (release) =>
        release.tags.length === 0 ? (
          <span className="text-muted-foreground">None</span>
        ) : (
          <span className="font-mono text-[0.9em] wrap-break-word">
            {release.tags.join(", ")}
          </span>
        ),
    },
    {
      key: "created",
      header: "Created",
      cell: (release) => (
        <div className="flex flex-col gap-1">
          <RelativeTime value={release.created_at} />
          <span className="text-sm text-muted-foreground wrap-break-word">
            by {release.created_by}
          </span>
        </div>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      isLabelHiddenWhenStacked: true,
      cell: (release) => (
        <ReleaseRowMenu
          app={app}
          release={release}
          actor={actor}
          actions={actions}
          isViewingSource={source.state.releaseNo === release.release_no}
          triggerRef={(element) => {
            if (element === null) {
              triggers.current.delete(release.release_no);
            } else {
              triggers.current.set(release.release_no, element);
            }
          }}
          onSelect={choose}
        />
      ),
    },
  ];

  return (
    <Section
      title="Deployments"
      actions={
        <UpdateButton
          availability={update}
          actions={actions}
          onUpdate={controls.runUpdate}
        />
      }
    >
      <div className="flex flex-col gap-4">
        {updateReasons.length === 0 ? null : (
          <p className="text-sm text-muted-foreground">{updateReasons[0]}</p>
        )}
        {controls.failure === null ? null : (
          <ErrorNotice
            error={controls.failure.error}
            context="Could not create a release"
            onReload={onReload}
          />
        )}
        {failure === null ? null : (
          <ErrorNotice
            error={failure.error}
            context={failure.context}
            onReload={onReload}
          />
        )}
        <DataTable
          caption="Releases of this app, newest first"
          isCaptionHidden
          columns={columns}
          rows={releases}
          rowKey={(release) => String(release.release_no)}
          empty={
            <EmptyState title="No releases yet">
              Press Update to fetch the source folder and build the first
              release.
            </EmptyState>
          }
        />
        {openDeleteNo === null ? null : (
          <ReleaseDeleteConfirm
            key={openDeleteNo}
            releaseNo={openDeleteNo}
            onCancel={() => {
              setDeletingNo(null);
              focusTrigger(openDeleteNo);
            }}
            onConfirm={() => {
              const target = app.releases.find(
                (release) => release.release_no === openDeleteNo,
              );
              setDeletingNo(null);
              focusTrigger(openDeleteNo);
              if (target !== undefined) void write("delete", target);
            }}
          />
        )}
        {editedRelease === null || editing === null ? null : (
          <ReleaseEditForm
            // Another release, or the other field, gets a fresh form; a poll of the same one does not reset it.
            key={`${String(editedRelease.release_no)}:${editing.field}`}
            app={app}
            release={editedRelease}
            actions={actions}
            focusField={editing.field}
            onClose={() => {
              setEditing(null);
              focusTrigger(editedRelease.release_no);
            }}
            onReload={onReload}
          />
        )}
        {source.state.releaseNo === null ? null : (
          <SourceViewerPanel
            slug={app.slug}
            viewer={{
              ...source,
              close: () => {
                const releaseNo = source.state.releaseNo;
                source.close();
                focusTrigger(releaseNo);
              },
            }}
            onReloadApp={onReload}
          />
        )}
      </div>
    </Section>
  );
}
