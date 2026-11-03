"use client";

import { useState } from "react";

import type { AppDetail } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { emptyAttachDraft } from "@/lib/app/resources";
import type { AttachDraft } from "@/lib/app/resources";
import { RequirementsSection } from "./requirements-section";
import { useResourceTypes } from "./resources/use-resource-types";
import { ResourcesSection } from "./resources-section";
import type { AppActions } from "./use-app-actions";

export interface ResourceAreaProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  onReload: () => void;
}

interface OpenDraft {
  draft: AttachDraft;
  /** Counts the drafts opened, so choosing another requirement restarts the panel. */
  id: number;
}

/**
 * The Requirements and the Resources sections share the catalogue of types
 * and the Add panel: an Add button in the first opens the panel in the
 * second, filled in.
 */
export function ResourceArea({
  app,
  actor,
  actions,
  onReload,
}: ResourceAreaProps) {
  const types = useResourceTypes();
  const [open, setOpen] = useState<OpenDraft | null>(null);
  const catalogue =
    types.state.status === "ready" ? types.state.catalogue : null;

  function openWith(draft: AttachDraft): void {
    setOpen((current) => ({ draft, id: (current?.id ?? 0) + 1 }));
  }

  return (
    <>
      <RequirementsSection
        app={app}
        actor={actor}
        catalogue={catalogue}
        onAdd={openWith}
      />
      <ResourcesSection
        app={app}
        actor={actor}
        actions={actions}
        onReload={onReload}
        types={types.state}
        onRetryTypes={types.reload}
        draft={open?.draft ?? null}
        draftId={open?.id ?? 0}
        onOpenPanel={() => {
          openWith(emptyAttachDraft());
        }}
        onClosePanel={() => {
          setOpen(null);
        }}
      />
    </>
  );
}
