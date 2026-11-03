"use client";

import { Command as CommandPrimitive } from "cmdk";
import {
  ChevronRightIcon,
  ChevronsUpDownIcon,
  CornerDownLeftIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { ResourceSuggestionQuery, suggestResources } from "@/lib/api";
import type {
  ResourceSuggestion,
  ResourceSuggestionList,
  ResourceTypeEntry,
} from "@/lib/api";
import { SUGGESTION_DELAY_MS, createDebouncedSearch } from "@/lib/app/forms";
import type { DebouncedSearch, SearchOutcome } from "@/lib/app/forms";
import { isScopePrefix } from "@/lib/app/resources";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ResourceTypeIcon } from "./resource-type-icon";

const UNAVAILABLE_NOTE =
  "The list could not be read just now. Type it in full; the attach check still runs.";

/**
 * How the field works for this type: `listing` searches the person's objects,
 * `typing` is a plain text field (the type has no list, or Databricks would
 * not give one), `probing` while the first answer says which.
 */
type FieldMode =
  | { kind: "probing" }
  | { kind: "listing"; first: ResourceSuggestionList }
  | { kind: "typing"; note: string };

type SearchState =
  | SearchOutcome<ResourceSuggestionList>
  /** Text the query cannot carry: only the Use row. */
  | { status: "skipped"; text: string };

function isQueryable(type: string, text: string): boolean {
  return ResourceSuggestionQuery.safeParse({ type, q: text }).success;
}

function noteForTyping(entry: ResourceTypeEntry): string {
  return `There is no list of these to choose from; type it, such as ${entry.target_hint}.`;
}

export interface ResourceObjectFieldProps {
  /** From `Field`: ties the label to the trigger or the text box. */
  id?: string;
  entry: ResourceTypeEntry;
  target: string;
  /** The chosen object's name, shown with its target; `null` for a typed target. */
  objectName: string | null;
  onPick: (suggestion: ResourceSuggestion) => void;
  onType: (target: string) => void;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
  "aria-required"?: true;
}

/**
 * The object of the Add panel, as in the Databricks console: a list of what
 * the person can see, read with their token as they type (debounced, the call
 * before aborted). For a secret the list first offers scopes; choosing one
 * goes on to its keys. When the type has no list, or Databricks gives none,
 * it is a text field with the server's note and the type's example.
 * Typing a target in full always works.
 */
export function ResourceObjectField({
  id,
  entry,
  target,
  objectName,
  onPick,
  onType,
  ...aria
}: ResourceObjectFieldProps) {
  const [mode, setMode] = useState<FieldMode>(
    entry.can_suggest
      ? { kind: "probing" }
      : { kind: "typing", note: noteForTyping(entry) },
  );
  const [isOpen, setIsOpen] = useState(false);
  const [text, setText] = useState("");
  const [state, setState] = useState<SearchState>({
    status: "skipped",
    text: "",
  });
  const [lastList, setLastList] = useState<ResourceSuggestionList | null>(null);
  // The highlighted row (Enter picks it). Held here so an answer that arrives
  // after the typing moves it to the first object: on its own, the list would
  // keep the Use row it highlighted while it was the only row.
  const [highlighted, setHighlighted] = useState("");
  const searchRef = useRef<DebouncedSearch | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const noteId = useId();
  const { type } = entry;

  // The first answer decides between the list and the text field. The parent
  // keys this field by type, so a new type starts here again.
  useEffect(() => {
    if (!entry.can_suggest) return undefined;
    const controller = new AbortController();
    suggestResources({ type }, { signal: controller.signal }).then(
      (list) => {
        if (controller.signal.aborted) return;
        if (list.source === "caller_token") {
          setMode({ kind: "listing", first: list });
          setLastList(list);
        } else {
          setMode({ kind: "typing", note: list.note ?? noteForTyping(entry) });
        }
      },
      () => {
        // An aborted probe means the field left; any other failure leaves typing.
        if (!controller.signal.aborted) {
          setMode({ kind: "typing", note: UNAVAILABLE_NOTE });
        }
      },
    );
    return () => {
      controller.abort();
    };
  }, [entry, type]);

  useEffect(() => {
    const search = createDebouncedSearch<ResourceSuggestionList>({
      delayMs: SUGGESTION_DELAY_MS,
      run: (query, signal) => suggestResources({ type, q: query }, { signal }),
      onOutcome: (outcome) => {
        setState(outcome);
        if (outcome.status === "done") {
          setLastList(outcome.result);
          const [first] = outcome.result.items;
          if (first !== undefined) setHighlighted(`object:${first.target}`);
        }
      },
    });
    searchRef.current = search;
    return () => {
      search.cancel();
      searchRef.current = null;
    };
  }, [type]);

  if (mode.kind === "typing") {
    const describedBy = [aria["aria-describedby"], noteId]
      .filter(Boolean)
      .join(" ");
    return (
      <div className="flex w-full max-w-xl flex-col gap-1">
        <Input
          id={id}
          {...aria}
          aria-describedby={describedBy}
          name="target"
          type="text"
          value={target}
          placeholder={entry.target_hint}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          className="font-mono"
          onChange={(event) => {
            onType(event.target.value);
          }}
        />
        <p id={noteId} className="text-sm text-muted-foreground">
          {mode.note}
        </p>
      </div>
    );
  }

  function changeText(next: string): void {
    setText(next);
    const query = next.trim();
    if (isQueryable(type, query)) {
      searchRef.current?.search(query);
    } else {
      searchRef.current?.cancel();
      setState({ status: "skipped", text: query });
    }
  }

  function changeOpen(next: boolean): void {
    setIsOpen(next);
    searchRef.current?.cancel();
    setText("");
    // Opening shows the first list again, without a call.
    setState({ status: "skipped", text: "" });
    if (mode.kind === "listing") setLastList(mode.first);
  }

  function pick(suggestion: ResourceSuggestion): void {
    if (isScopePrefix(type, suggestion)) {
      // A scope is the first half of a secret: go on to its keys.
      changeText(suggestion.target);
      return;
    }
    onPick(suggestion);
    changeOpen(false);
    triggerRef.current?.focus();
  }

  function chooseTyped(): void {
    onType(text.trim());
    changeOpen(false);
    triggerRef.current?.focus();
  }

  function clear(): void {
    onType("");
    triggerRef.current?.focus();
  }

  const isLoading = state.status === "loading";
  const isSkipped = state.status === "skipped" && text.trim() !== "";
  const list = isSkipped ? null : lastList;
  const items = list?.items ?? [];
  const typed = text.trim();
  const canUseTyped =
    typed !== "" &&
    !(type === "secret" && typed.endsWith("/")) &&
    !items.some((item) => item.target === typed);
  const note =
    state.status === "failed"
      ? UNAVAILABLE_NOTE
      : (list?.note ??
        (list?.truncated === true
          ? "More match than are shown; type more of the name."
          : null));
  const isProbing = mode.kind === "probing";

  return (
    <div className="flex w-full max-w-xl min-w-0 items-center gap-2">
      <Popover open={isOpen} onOpenChange={changeOpen}>
        <PopoverTrigger asChild>
          <Button
            ref={triggerRef}
            id={id}
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={isOpen}
            aria-haspopup="listbox"
            aria-busy={isProbing || undefined}
            disabled={isProbing}
            data-placeholder={target === "" ? "" : undefined}
            className="h-auto min-h-10 min-w-0 flex-1 justify-between py-1.5 font-normal data-[placeholder]:text-muted-foreground"
            {...aria}
          >
            {isProbing ? (
              <span className="truncate">Loading the list…</span>
            ) : target === "" ? (
              <span className="truncate">Choose or type the {entry.label}</span>
            ) : (
              <span className="flex min-w-0 items-center gap-2 rounded-sm bg-secondary px-2 py-0.5 text-secondary-foreground">
                <ResourceTypeIcon type={type} />
                {objectName === null ? null : (
                  <span className="truncate">{objectName}</span>
                )}
                <span className="truncate font-mono text-xs">{target}</span>
              </span>
            )}
            <ChevronsUpDownIcon
              className="text-muted-foreground"
              aria-hidden="true"
            />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-(--radix-popover-trigger-width) min-w-72 p-0"
        >
          <Command
            label={`Choose the ${entry.label}`}
            shouldFilter={false}
            loop
            value={highlighted}
            onValueChange={setHighlighted}
          >
            <CommandInput
              placeholder={
                type === "secret"
                  ? "Type a scope, then / and a key"
                  : "Search by name, or type it in full"
              }
              value={text}
              onValueChange={changeText}
              autoCapitalize="none"
              autoComplete="off"
              spellCheck={false}
            />
            <CommandList aria-busy={isLoading || undefined}>
              {isLoading ? (
                <CommandPrimitive.Loading
                  label="Searching"
                  className="px-3 py-4 text-sm text-muted-foreground"
                >
                  Searching…
                </CommandPrimitive.Loading>
              ) : null}
              {!isLoading && items.length === 0 && !canUseTyped ? (
                <p className="px-3 py-4 text-sm text-muted-foreground">
                  Nothing to show yet. Type a name, or the {entry.label} in
                  full, such as {entry.target_hint}.
                </p>
              ) : null}
              {items.map((item) => {
                const isScope = isScopePrefix(type, item);
                return (
                  <CommandItem
                    key={item.target}
                    value={`object:${item.target}`}
                    aria-label={
                      isScope ? `${item.label}, open its keys` : item.label
                    }
                    onSelect={() => {
                      pick(item);
                    }}
                    className="gap-2"
                  >
                    <ResourceTypeIcon type={type} />
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate">{item.label}</span>
                      {item.detail === null ? null : (
                        <span className="truncate text-xs text-muted-foreground">
                          {item.detail}
                        </span>
                      )}
                    </span>
                    {isScope ? (
                      <ChevronRightIcon
                        aria-hidden="true"
                        className="ml-auto text-muted-foreground"
                      />
                    ) : (
                      <span className="ml-auto truncate font-mono text-xs text-muted-foreground">
                        {item.target}
                      </span>
                    )}
                  </CommandItem>
                );
              })}
              {canUseTyped ? (
                <CommandItem
                  value={`typed:${typed}`}
                  aria-label={`Use “${typed}”`}
                  onSelect={chooseTyped}
                  className="gap-2"
                >
                  <span className="truncate">
                    Use <span className="font-mono">“{typed}”</span>
                  </span>
                  <CornerDownLeftIcon
                    aria-hidden="true"
                    className="ml-auto text-muted-foreground"
                  />
                </CommandItem>
              ) : null}
              {note === null ? null : (
                <p className="border-t px-3 py-2 text-xs text-muted-foreground">
                  {note}
                </p>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {target === "" || isProbing ? null : (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={`Clear ${objectName ?? target}`}
          onClick={clear}
        >
          <XIcon aria-hidden="true" />
        </Button>
      )}
    </div>
  );
}
