"use client";

import { Command as CommandPrimitive } from "cmdk";
import {
  ArrowUpIcon,
  BoxIcon,
  FileIcon,
  FolderIcon,
  FolderOpenIcon,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, Ref } from "react";

import { browseWorkspace } from "@/lib/api";
import type { WorkspaceListing, WorkspaceRoot } from "@/lib/api";
import {
  breadcrumb,
  browseRefusal,
  folderHint,
  orderEntries,
  startPath,
  upTarget,
} from "@/lib/folder-browser";
import { Button } from "./button";
import type { FieldControlProps } from "./field";
import { LiveRegion } from "./live-region";
import { Notice } from "./notice";
import { Badge } from "./ui/badge";
import { Button as UiButton } from "./ui/button";
import { Input } from "./ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { Skeleton } from "./ui/skeleton";

/** The hint of a source-folder field for a person who cannot browse: the route answers them 403. */
export const TYPE_PATH_HINT =
  "Type the workspace folder the app is read from, starting with /Workspace/ (for example /Workspace/Users/you@example.com/my-app).";

/** The hint of a source-folder field with the browser beside it. */
export const BROWSE_PATH_HINT =
  "The workspace folder the app is read from, starting with /Workspace/. Type it, or pick it with Browse.";

const SKELETON_ROWS = 5;
const ROW_CLASSES =
  "flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-hidden select-none data-[disabled=true]:pointer-events-none data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground [&_svg]:size-4 [&_svg]:shrink-0";

export interface SourceFolderInputProps {
  /** What `Field` hands its control: the id, the hint and the error. */
  control: FieldControlProps;
  value: string;
  onValueChange: (value: string) => void;
  /** Offer Browse: a platform admin or a person who manages an app (`mayBrowseWorkspace`). */
  canBrowse: boolean;
  /** Fills the form's memory field with the figure a folder suggests; leave it out when the form has none. */
  onUseMemory?: (memoryMb: number) => void;
  name?: string;
  inputRef?: Ref<HTMLInputElement>;
}

/**
 * The source-folder text field with the folder browser beside it
 * (docs/product/admin-ui-design.md, Components: Folder browser). The field
 * stays editable whatever the browser does: Browse only fills it, so a person
 * whose listing is refused can still type the path.
 */
export function SourceFolderInput({
  control,
  value,
  onValueChange,
  canBrowse,
  onUseMemory,
  name,
  inputRef,
}: SourceFolderInputProps) {
  const [announcement, setAnnouncement] = useState<string | null>(null);
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex min-w-0 gap-2">
        <Input
          {...control}
          ref={inputRef}
          name={name}
          type="text"
          className="min-w-0 flex-1"
          value={value}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          onChange={(event) => {
            onValueChange(event.target.value);
          }}
        />
        {canBrowse ? (
          <FolderBrowser
            fieldValue={value}
            onPick={(path) => {
              onValueChange(path);
              setAnnouncement(`Source folder set to ${path}`);
            }}
            onUseMemory={
              onUseMemory === undefined
                ? undefined
                : (memoryMb) => {
                    onUseMemory(memoryMb);
                    setAnnouncement(`Memory budget set to ${memoryMb} MB`);
                  }
            }
          />
        ) : null}
      </div>
      <LiveRegion message={announcement} />
    </div>
  );
}

export interface FolderBrowserProps {
  /** The field's text: the browser opens at the folder it names, else at the roots. */
  fieldValue: string;
  onPick: (path: string) => void;
  onUseMemory?: (memoryMb: number) => void;
}

/**
 * Browse beside a source-folder field: a popover that lists one workspace
 * folder at a time with `GET /workspace/browse`. Arrows move, Enter opens a
 * folder, Backspace goes up, Escape closes. The list mounts on each opening,
 * so it starts again from the field's text.
 */
export function FolderBrowser({
  fieldValue,
  onPick,
  onUseMemory,
}: FolderBrowserProps) {
  const [isOpen, setIsOpen] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  return (
    <Popover open={isOpen} onOpenChange={setIsOpen}>
      <PopoverTrigger asChild>
        <Button aria-haspopup="dialog" aria-expanded={isOpen}>
          <FolderOpenIcon aria-hidden="true" className="size-4" />
          Browse
        </Button>
      </PopoverTrigger>
      {isOpen ? (
        <PopoverContent
          align="end"
          aria-label="Browse the workspace"
          className="flex w-[min(28rem,calc(100vw-2rem))] flex-col gap-3 p-3"
          onOpenAutoFocus={(event) => {
            // The list takes the keys; Radix would focus the first button.
            event.preventDefault();
            listRef.current?.focus();
          }}
        >
          <BrowserPanel
            start={startPath(fieldValue)}
            listRef={listRef}
            onPick={(path) => {
              onPick(path);
              setIsOpen(false);
            }}
            onUseMemory={
              onUseMemory === undefined
                ? undefined
                : (memoryMb) => {
                    onUseMemory(memoryMb);
                  }
            }
          />
        </PopoverContent>
      ) : null}
    </Popover>
  );
}

type Fetched =
  { key: string; listing: WorkspaceListing } | { key: string; error: unknown };

function BrowserPanel({
  start,
  listRef,
  onPick,
  onUseMemory,
}: {
  start: string | null;
  listRef: Ref<HTMLDivElement>;
  onPick: (path: string) => void;
  onUseMemory: ((memoryMb: number) => void) | undefined;
}) {
  const [target, setTarget] = useState<string | null>(start);
  const [attempt, setAttempt] = useState(0);
  const [fetched, setFetched] = useState<Fetched | null>(null);
  const [roots, setRoots] = useState<readonly WorkspaceRoot[]>([]);
  const titleId = useId();
  const key = `${attempt}:${target ?? ""}`;

  // The roots name the crumbs ("My folder" instead of the person's address).
  // Listing them calls nothing upstream, so it is asked once on opening.
  useEffect(() => {
    const controller = new AbortController();
    browseWorkspace({}, { signal: controller.signal }).then(
      (listing) => {
        setRoots(listing.roots);
      },
      () => {
        // Best effort: without the roots a crumb shows the folder's own name,
        // and the listing below reports its own refusal.
      },
    );
    return () => {
      controller.abort();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    browseWorkspace(target === null ? {} : { path: target }, {
      signal: controller.signal,
    }).then(
      (listing) => {
        setFetched({ key, listing });
        if (listing.path === null) setRoots(listing.roots);
      },
      (error: unknown) => {
        if (!controller.signal.aborted) setFetched({ key, error });
      },
    );
    return () => {
      controller.abort();
    };
  }, [key, target]);

  const current = fetched?.key === key ? fetched : null;
  const listing =
    current !== null && "listing" in current ? current.listing : null;
  const refusal =
    current !== null && "error" in current
      ? browseRefusal(current.error)
      : null;
  const crumbs = breadcrumb(target, roots);
  const folderAbove = crumbs.at(-2)?.path ?? null;
  const up = listing === null ? null : upTarget(listing);
  const hint = listing === null ? null : folderHint(listing);
  const canGoUp = target !== null;
  // Only a folder that was listed can be used: a refused one may not exist.
  const pickable = listing?.path ?? null;

  function open(path: string | null): void {
    setTarget(path);
  }

  function goUp(): void {
    if (!canGoUp) return;
    open(up === null ? folderAbove : up.path);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Backspace" && canGoUp) {
      event.preventDefault();
      goUp();
    }
  }

  return (
    <>
      <div className="flex flex-col gap-2">
        <p id={titleId} className="text-sm font-semibold">
          Pick the app&apos;s source folder
        </p>
        <nav
          aria-label="Folder path"
          className="flex min-w-0 items-center gap-1"
        >
          <UiButton
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Up one folder"
            title="Up one folder"
            disabled={!canGoUp}
            onClick={goUp}
          >
            <ArrowUpIcon aria-hidden="true" />
          </UiButton>
          <ol className="flex min-w-0 flex-wrap items-center gap-x-1 text-sm">
            {crumbs.map((crumb, index) => {
              const isLast = index === crumbs.length - 1;
              return (
                <li
                  key={crumb.path ?? ""}
                  className="flex min-w-0 items-center gap-1"
                >
                  {index === 0 ? null : (
                    <span aria-hidden="true" className="text-muted-foreground">
                      /
                    </span>
                  )}
                  {isLast ? (
                    <span
                      aria-current="location"
                      className="truncate font-medium"
                    >
                      {crumb.label}
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="truncate text-link underline-offset-4 hover:underline"
                      onClick={() => {
                        open(crumb.path);
                      }}
                    >
                      {crumb.label}
                    </button>
                  )}
                </li>
              );
            })}
          </ol>
        </nav>
      </div>

      <CommandPrimitive
        label="Folders"
        shouldFilter={false}
        loop
        onKeyDown={handleKeyDown}
        className="flex min-h-0 flex-col"
      >
        <CommandPrimitive.List
          ref={listRef}
          aria-labelledby={titleId}
          className="max-h-64 min-h-24 overflow-y-auto rounded-md border p-1 outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        >
          {current === null ? <SkeletonRows /> : null}
          {listing !== null && listing.path === null
            ? listing.roots.map((root) => (
                <CommandPrimitive.Item
                  key={root.path}
                  value={root.path}
                  className={ROW_CLASSES}
                  onSelect={() => {
                    open(root.path);
                  }}
                >
                  <FolderIcon aria-hidden="true" />
                  <span className="font-medium">{root.label}</span>
                  <span className="min-w-0 truncate text-muted-foreground">
                    {root.path}
                  </span>
                </CommandPrimitive.Item>
              ))
            : null}
          {listing !== null && listing.path !== null
            ? orderEntries(listing.entries).map((entry) =>
                entry.kind === "folder" ? (
                  <CommandPrimitive.Item
                    key={entry.path}
                    value={entry.path}
                    className={ROW_CLASSES}
                    onSelect={() => {
                      open(entry.path);
                    }}
                  >
                    <FolderIcon aria-hidden="true" />
                    <span className="min-w-0 truncate">{entry.name}</span>
                  </CommandPrimitive.Item>
                ) : (
                  <CommandPrimitive.Item
                    key={entry.path}
                    value={entry.path}
                    disabled
                    className={`${ROW_CLASSES} text-muted-foreground`}
                  >
                    <FileIcon aria-hidden="true" />
                    <span className="min-w-0 truncate">{entry.name}</span>
                    <span className="sr-only">(a file)</span>
                  </CommandPrimitive.Item>
                ),
              )
            : null}
        </CommandPrimitive.List>
      </CommandPrimitive>

      <p role="status" className="sr-only">
        {current === null ? "Loading the folder" : null}
      </p>

      {listing !== null &&
      listing.path !== null &&
      listing.entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          This folder is empty. Use it as it is, or go up and pick another.
        </p>
      ) : null}

      {refusal === null ? null : (
        <Notice tone="danger" title={refusal.title} live="assertive">
          <div className="flex flex-col gap-2">
            {refusal.detail === null ? null : (
              <p className="wrap-break-word">
                The server said: {refusal.detail}
              </p>
            )}
            <p>{refusal.hint}</p>
            <RecoveryAction
              recovery={refusal.recovery}
              onRetry={() => {
                setAttempt((previous) => previous + 1);
              }}
              onStart={() => {
                open(null);
              }}
              onUp={goUp}
            />
          </div>
        </Notice>
      )}

      {listing?.truncated === true ? (
        <p className="text-sm text-muted-foreground">
          This folder holds more than {listing.entries.length} entries; only the
          first {listing.entries.length} are listed. Type the rest of the path
          in the field.
        </p>
      ) : null}

      {listing === null || hint === null ? null : (
        <div className="flex flex-col gap-2 rounded-md border border-tone-info-border bg-tone-info-bg p-2 text-sm text-tone-info-text">
          <p className="flex items-start gap-2">
            <BoxIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
            <span>{hint.text}</span>
          </p>
          {listing.markers.length === 0 ? null : (
            <ul
              aria-label="Files that mark it"
              className="flex flex-wrap gap-1"
            >
              {listing.markers.map((marker) => (
                <li key={marker}>
                  <Badge variant="outline" className="font-mono">
                    {marker}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
          {hint.suggestedMemoryMb === null ||
          onUseMemory === undefined ? null : (
            <div>
              <Button
                size="small"
                onClick={() => {
                  if (hint.suggestedMemoryMb !== null) {
                    onUseMemory(hint.suggestedMemoryMb);
                  }
                }}
              >
                Use {hint.suggestedMemoryMb} MB
              </Button>
            </div>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        {target === null ? (
          <p className="mr-auto text-sm text-muted-foreground">
            Open a folder first.
          </p>
        ) : null}
        <Button
          variant="primary"
          size="small"
          disabled={pickable === null}
          onClick={() => {
            if (pickable !== null) onPick(pickable);
          }}
        >
          Use this folder
        </Button>
      </div>
    </>
  );
}

function RecoveryAction({
  recovery,
  onRetry,
  onStart,
  onUp,
}: {
  recovery: ReturnType<typeof browseRefusal>["recovery"];
  onRetry: () => void;
  onStart: () => void;
  onUp: () => void;
}) {
  switch (recovery) {
    case "reload-page":
      return (
        <div>
          <Button
            size="small"
            onClick={() => {
              window.location.reload();
            }}
          >
            Reload
          </Button>
        </div>
      );
    case "retry":
      return (
        <div>
          <Button size="small" onClick={onRetry}>
            Retry
          </Button>
        </div>
      );
    case "start":
      return (
        <div>
          <Button size="small" onClick={onStart}>
            Start
          </Button>
        </div>
      );
    case "up":
      return (
        <div>
          <Button size="small" onClick={onUp}>
            Up
          </Button>
        </div>
      );
    case "type":
      return null;
  }
}

function SkeletonRows() {
  return (
    <div aria-hidden="true" className="flex flex-col gap-1 p-1">
      {Array.from({ length: SKELETON_ROWS }, (_, index) => (
        <div key={index} className="flex items-center gap-2 px-1 py-1.5">
          <Skeleton className="size-4 shrink-0" />
          <Skeleton className={index % 2 === 0 ? "h-4 w-2/5" : "h-4 w-3/5"} />
        </div>
      ))}
    </div>
  );
}
