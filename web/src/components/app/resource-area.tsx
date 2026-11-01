"use client";

import { useState } from "react";

import type { AppDetail } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { emptyAttachDraft } from "@/lib/app/resources";
import type { AttachDraft } from "@/lib/app/resources";
import { RequirementsSection } from "./requirements-section";
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
  /** Counts the drafts opened, so choosing another requirement restarts the form. */
  id: number;
}

/**
 * The Requirements panel and the Resources section share one thing: the attach
 * form. An Attach button in the first opens the form in the second, filled in.
 */
export function ResourceArea({
  app,
  actor,
  actions,
  onReload,
}: ResourceAreaProps) {
  const [open, setOpen] = useState<OpenDraft | null>(null);

  function openWith(draft: AttachDraft): void {
    setOpen((current) => ({ draft, id: (current?.id ?? 0) + 1 }));
  }

  return (
    <>
      <RequirementsSection app={app} actor={actor} onAttach={openWith} />
      <ResourcesSection
        app={app}
        actor={actor}
        actions={actions}
        onReload={onReload}
        draft={open?.draft ?? null}
        draftId={open?.id ?? 0}
        onOpenForm={() => {
          openWith(emptyAttachDraft());
        }}
        onCloseForm={() => {
          setOpen(null);
        }}
      />
    </>
  );
}
