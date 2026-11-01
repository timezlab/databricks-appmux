"use client";

import { useState } from "react";

import {
  activateRelease,
  deleteRelease,
  lockRelease,
  unlockRelease,
} from "@/lib/api";
import type { AppDetail, ReleaseSummary } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { releaseActionMessage } from "@/lib/app/outcomes";
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
import { ReleaseActions } from "./release-actions";
import type { ReleaseWrite } from "./release-actions";
import { ReleaseEditForm } from "./release-edit-form";
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

/** The release list with its actions, and the edit and source panels that open under it. */
export function ReleaseSection({
  app,
  actor,
  actions,
  onReload,
}: ReleaseSectionProps) {
  const [editingNo, setEditingNo] = useState<number | null>(null);
  const [failure, setFailure] = useState<{
    context: string;
    error: unknown;
  } | null>(null);
  const releases = releasesNewestFirst(app.releases);
  const releaseNos = app.releases.map((release) => release.release_no);
  const source = useSourceViewer(app.slug, releaseNos);

  // A poll can delete the release a panel is open on; the panel goes with it.
  const openEditNo = reconcileSelection(editingNo, app.releases);
  const editedRelease =
    app.releases.find((release) => release.release_no === openEditNo) ?? null;

  async function write(
    action: ReleaseWrite,
    release: ReleaseSummary,
  ): Promise<void> {
    setFailure(null);
    const releaseNo = release.release_no;
    const result = await actions.run(
      `${action}:${releaseNo}`,
      () => writeCall(action, app.slug, releaseNo),
      releaseActionMessage(action, releaseNo),
    );
    if (!result.ok) {
      setFailure({
        context: `Could not ${action} release ${releaseNo}`,
        error: result.error,
      });
    }
  }

  const columns: DataTableColumn<ReleaseSummary>[] = [
    {
      key: "release",
      header: "Release",
      isRowHeader: true,
      cell: (release) => {
        const markers = releaseMarkers(release);
        return (
          <div className="flex flex-col gap-1">
            <div>
              <span className="whitespace-nowrap">#{release.release_no}</span>
              {release.name === null ? null : (
                <span className="wrap-break-word">, {release.name}</span>
              )}
            </div>
            {markers.length === 0 ? null : (
              <div className="text-muted-foreground">{markers.join(", ")}</div>
            )}
          </div>
        );
      },
    },
    {
      key: "status",
      header: "Status",
      cell: (release) => (
        <div className="flex flex-col gap-1">
          <div>
            <ReleaseStatusBadge status={release.status} />
          </div>
          {release.status_reason === null ? null : (
            <div className="text-muted-foreground wrap-break-word">
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
      key: "lock",
      header: "Lock",
      cell: (release) => {
        const label = releaseLockLabel(release);
        return label === null ? (
          <span className="text-muted-foreground">None</span>
        ) : (
          <span className="wrap-break-word">{label}</span>
        );
      },
    },
    {
      key: "created",
      header: "Created",
      cell: (release) => (
        <div className="flex flex-col gap-1">
          <RelativeTime value={release.created_at} />
          <span className="text-muted-foreground wrap-break-word">
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
        <ReleaseActions
          app={app}
          release={release}
          actor={actor}
          actions={actions}
          isEditing={openEditNo === release.release_no}
          isViewingSource={source.state.releaseNo === release.release_no}
          onWrite={(action, target) => {
            void write(action, target);
          }}
          onToggleEdit={(target) => {
            setEditingNo(
              openEditNo === target.release_no ? null : target.release_no,
            );
          }}
          onToggleSource={(target) => {
            if (source.state.releaseNo === target.release_no) {
              source.close();
            } else {
              source.open(target.release_no);
            }
          }}
        />
      ),
    },
  ];

  return (
    <Section title="Releases">
      <div className="flex flex-col gap-4">
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
        {editedRelease === null ? null : (
          <ReleaseEditForm
            // A new release gets a fresh form; a poll of the same one does not reset it.
            key={editedRelease.release_no}
            app={app}
            release={editedRelease}
            actions={actions}
            onClose={() => {
              setEditingNo(null);
            }}
            onReload={onReload}
          />
        )}
        {source.state.releaseNo === null ? null : (
          <SourceViewerPanel
            slug={app.slug}
            viewer={source}
            onReloadApp={onReload}
          />
        )}
      </div>
    </Section>
  );
}
