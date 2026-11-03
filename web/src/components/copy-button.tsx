"use client";

import { CheckIcon, CopyIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { clipboardText } from "@/lib/clipboard-text";
import { LiveRegion } from "./live-region";
import { Button as UiButton } from "./ui/button";

/** How long the check mark stays after a copy, before the copy icon comes back. */
const COPIED_MARK_MS = 2000;

export interface CopyButtonProps {
  /** The text that goes to the clipboard, less its control and bidirectional formatting characters (`clipboardText` says which). */
  value: string;
  /** What is copied, for the button's name: "the route" reads "Copy the route". */
  label: string;
  /**
   * The id of the element that shows `value`. When the clipboard cannot be
   * written (no Clipboard API, a page outside a secure context, a refusal),
   * its text is selected instead, so the keyboard can copy it.
   */
  selectTargetId?: string;
}

/** Selects the text of the element `id` names; false when there is none. */
function selectText(id: string | undefined): boolean {
  const target = id === undefined ? null : document.getElementById(id);
  if (target === null) return false;
  if (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement
  ) {
    target.select();
    return true;
  }
  const selection = window.getSelection();
  if (selection === null) return false;
  selection.selectAllChildren(target);
  return true;
}

/**
 * An icon button that copies `value` and says so: the icon turns into a check
 * mark, and a status region announces "Copied". When the clipboard cannot be
 * written it selects the text instead and says how to copy it.
 */
export function CopyButton({ value, label, selectTargetId }: CopyButtonProps) {
  const [message, setMessage] = useState<string | null>(null);
  const [isCopied, setIsCopied] = useState(false);

  useEffect(() => {
    if (!isCopied) return;
    const timer = window.setTimeout(() => {
      setIsCopied(false);
    }, COPIED_MARK_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [isCopied]);

  async function copy(): Promise<void> {
    try {
      // `navigator.clipboard` is undefined outside a secure context, whatever the types say.
      const clipboard = navigator.clipboard as Clipboard | undefined;
      if (clipboard === undefined) throw new Error("no Clipboard API");
      await clipboard.writeText(clipboardText(value));
      setIsCopied(true);
      setMessage("Copied");
    } catch {
      // Handled by the fallback: select the text for the keyboard, and say so.
      setIsCopied(false);
      setMessage(
        selectText(selectTargetId)
          ? "Could not copy. The text is selected: press Ctrl+C (⌘C on a Mac) to copy it."
          : "Could not copy. Select the text and copy it with the keyboard.",
      );
    }
  }

  return (
    <>
      <UiButton
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
        onClick={() => {
          void copy();
        }}
      >
        {isCopied ? (
          <CheckIcon aria-hidden="true" />
        ) : (
          <CopyIcon aria-hidden="true" />
        )}
      </UiButton>
      <LiveRegion message={message} />
    </>
  );
}
