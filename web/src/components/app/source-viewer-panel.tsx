"use client";

import {
  SOURCE_AUDIT_NOTICE,
  previewFile,
  sourceEntries,
  sourceListingSummary,
} from "@/lib/app/source";
import type { SourceViewState } from "@/lib/app/source";
import { formatBytes } from "@/lib/format";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/button";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Loading } from "@/components/loading";
import { Notice } from "@/components/notice";
import type { SourceViewer } from "./use-source-viewer";

export interface SourceViewerPanelProps {
  slug: string;
  viewer: SourceViewer;
  /** Reads the app again: offered after a 409 or a 404. */
  onReloadApp: () => void;
}

function Listing({
  releaseNo,
  state,
  viewer,
  onReloadApp,
}: {
  releaseNo: number;
  state: SourceViewState;
  viewer: SourceViewer;
  onReloadApp: () => void;
}) {
  const { listing } = state;
  switch (listing.status) {
    case "idle":
    case "loading":
      return <Loading label="Loading the file list" />;
    case "failed":
      return (
        <ErrorNotice
          error={listing.error}
          context="Could not list the files"
          onReload={onReloadApp}
          onRetry={() => {
            viewer.open(releaseNo);
          }}
        />
      );
    case "ready": {
      const entries = sourceEntries(listing.value);
      if (entries.length === 0) {
        return <EmptyState title="This snapshot holds no files" />;
      }
      return (
        <div className="flex flex-col gap-2">
          <p className="text-muted-foreground">
            {sourceListingSummary(listing.value)}
          </p>
          <ul className="max-h-96 divide-y overflow-auto rounded-md border">
            {entries.map((entry) => (
              <li
                key={entry.path}
                className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 py-2"
              >
                <span className="min-w-0 wrap-break-word">
                  {entry.directory === "" ? null : (
                    <span className="text-muted-foreground">
                      {entry.directory}/
                    </span>
                  )}
                  <span className="font-mono text-[0.9em]">{entry.name}</span>{" "}
                  <span className="text-muted-foreground">
                    {formatBytes(entry.sizeBytes)}
                  </span>
                </span>
                {entry.canOpen ? (
                  <Button
                    size="small"
                    onClick={() => {
                      viewer.openFile(releaseNo, entry.path);
                    }}
                  >
                    Open <span className="sr-only">{entry.path}</span>
                  </Button>
                ) : (
                  <span className="text-muted-foreground">{entry.reason}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      );
    }
  }
}

function FileView({
  releaseNo,
  state,
  viewer,
  onReloadApp,
}: {
  releaseNo: number;
  state: SourceViewState;
  viewer: SourceViewer;
  onReloadApp: () => void;
}) {
  const { file, filePath } = state;
  switch (file.status) {
    case "idle":
    case "loading":
      return <Loading label="Loading the file" />;
    case "failed":
      return (
        <ErrorNotice
          error={file.error}
          context="Could not open the file"
          onReload={onReloadApp}
          onRetry={
            filePath === null
              ? undefined
              : () => {
                  viewer.openFile(releaseNo, filePath);
                }
          }
        />
      );
    case "ready": {
      const preview = previewFile(file.value);
      return (
        <div className="flex flex-col gap-2">
          <p className="text-muted-foreground">
            <span className="font-mono text-[0.9em] wrap-break-word">
              {file.value.path}
            </span>
            , {formatBytes(file.value.size_bytes)}
          </p>
          {preview.note === null ? null : (
            <Notice tone="info" title={preview.note} />
          )}
          {preview.isBinaryLooking ? null : (
            // File content is untrusted text: React escapes it, and it is
            // never given to anything that interprets it.
            <pre
              className="max-h-[32rem] overflow-auto bg-background text-sm whitespace-pre [tab-size:4]"
              role="region"
              aria-label={`Contents of ${file.value.path}`}
              tabIndex={0}
            >
              {preview.text}
            </pre>
          )}
        </div>
      );
    }
  }
}

/** The read-only view of one release's snapshot: the file list, then one file at a time. */
export function SourceViewerPanel({
  slug,
  viewer,
  onReloadApp,
}: SourceViewerPanelProps) {
  const { state } = viewer;
  const { releaseNo } = state;
  if (releaseNo === null) return null;

  return (
    <Card asChild>
      <section
        className="flex flex-col gap-4"
        aria-label={`Source of release ${releaseNo} of ${slug}`}
      >
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <h3 className="wrap-break-word">Source of release {releaseNo}</h3>
          <div className="flex flex-wrap items-center gap-2">
            {state.filePath === null ? null : (
              <Button size="small" onClick={viewer.closeFile}>
                Back to the file list
              </Button>
            )}
            <Button size="small" onClick={viewer.close}>
              Close
            </Button>
          </div>
        </div>
        <Notice tone="info" title={SOURCE_AUDIT_NOTICE} />
        {state.filePath === null ? (
          <Listing
            releaseNo={releaseNo}
            state={state}
            viewer={viewer}
            onReloadApp={onReloadApp}
          />
        ) : (
          <FileView
            releaseNo={releaseNo}
            state={state}
            viewer={viewer}
            onReloadApp={onReloadApp}
          />
        )}
      </section>
    </Card>
  );
}
