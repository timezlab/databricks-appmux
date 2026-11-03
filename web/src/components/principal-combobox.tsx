"use client";

import { Command as CommandPrimitive } from "cmdk";
import { cn } from "cn";
import {
  BotIcon,
  ChevronsUpDownIcon,
  CornerDownLeftIcon,
  GlobeIcon,
  UserIcon,
  UsersIcon,
  XIcon,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { searchPrincipals } from "@/lib/api";
import type { GrantType, PrincipalList } from "@/lib/api";
import { SUGGESTION_DELAY_MS, createDebouncedSearch } from "@/lib/app/forms";
import type { DebouncedSearch, SearchOutcome } from "@/lib/app/forms";
import {
  PRINCIPAL_TYPE_LABEL,
  isSearchablePrincipalText,
  principalRows,
  principalSuggestionsNote,
} from "@/lib/app/sharing";
import type { PrincipalChoice } from "@/lib/app/sharing";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

const PRINCIPAL_ICON: Readonly<Record<GrantType, LucideIcon>> = {
  user: UserIcon,
  group: UsersIcon,
  service_principal: BotIcon,
  everyone: GlobeIcon,
};

/** The icon of a principal type; its name is beside it in words, so the icon is hidden from screen readers. */
export function PrincipalIcon({
  type,
  className,
}: {
  type: GrantType;
  className?: string;
}) {
  const Icon = PRINCIPAL_ICON[type];
  return (
    <Icon
      aria-hidden="true"
      className={cn("size-4 shrink-0 text-muted-foreground", className)}
    />
  );
}

/** A principal on one line: icon, name, and the identifier in mono when it adds something. */
export function PrincipalLabel({ choice }: { choice: PrincipalChoice }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <PrincipalIcon type={choice.type} />
      <span className="sr-only">{PRINCIPAL_TYPE_LABEL[choice.type]}:</span>
      <span className="truncate">{choice.label}</span>
      {choice.identifier === null ? null : (
        <span className="truncate font-mono text-xs text-muted-foreground">
          {choice.identifier}
        </span>
      )}
    </span>
  );
}

type SearchState =
  | SearchOutcome<PrincipalList>
  /** Text the server cannot search (`*`, a long text): only the Use row. */
  | { status: "skipped"; text: string };

export interface PrincipalComboboxProps {
  /** From `Field`: ties the label to the trigger. */
  id?: string;
  value: PrincipalChoice | null;
  onValueChange: (choice: PrincipalChoice | null) => void;
  disabled?: boolean;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
  "aria-required"?: true;
}

/**
 * Who to share with, as in the Databricks console: type and get `everyone`,
 * groups, users and service principals from `GET /principals` (debounced, the
 * call before aborted), each with its icon and identifier. Free typing always
 * works: the text is offered as a "Use" row with its inferred type, and when a
 * directory cannot be read a quiet note says so. Built on Popover and Command
 * rather than `Combobox`, because it needs the typed row's type, the note and
 * the chosen principal drawn as a chip with a clear button.
 */
export function PrincipalCombobox({
  id,
  value,
  onValueChange,
  disabled = false,
  ...aria
}: PrincipalComboboxProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [text, setText] = useState("");
  const [state, setState] = useState<SearchState>({
    status: "skipped",
    text: "",
  });
  // The last list that arrived: kept while the next one loads, so the rows do
  // not flicker away at each keystroke.
  const [lastList, setLastList] = useState<PrincipalList | null>(null);
  // The highlighted row (Enter picks it). Held here so an answer that arrives
  // after the typing moves it to the first suggestion: on its own, the list
  // would keep the Use row it highlighted while it was the only row.
  const [highlighted, setHighlighted] = useState("");
  const searchRef = useRef<DebouncedSearch | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const search = createDebouncedSearch<PrincipalList>({
      delayMs: SUGGESTION_DELAY_MS,
      run: (query, signal) => searchPrincipals({ q: query }, { signal }),
      onOutcome: (outcome) => {
        setState(outcome);
        if (outcome.status === "done") {
          setLastList(outcome.result);
          const [first] = principalRows(outcome.result.items, outcome.text);
          if (first !== undefined) setHighlighted(first.id);
        }
      },
    });
    searchRef.current = search;
    return () => {
      search.cancel();
      searchRef.current = null;
    };
  }, []);

  function changeText(next: string): void {
    setText(next);
    const query = next.trim();
    if (isSearchablePrincipalText(query)) {
      searchRef.current?.search(query);
    } else {
      searchRef.current?.cancel();
      setState({ status: "skipped", text: query });
    }
  }

  function changeOpen(next: boolean): void {
    setIsOpen(next);
    if (next) {
      changeText("");
    } else {
      searchRef.current?.cancel();
      setText("");
    }
  }

  function choose(choice: PrincipalChoice): void {
    onValueChange(choice);
    changeOpen(false);
  }

  function clear(): void {
    onValueChange(null);
    triggerRef.current?.focus();
  }

  const isSkipped = state.status === "skipped";
  const suggestions = isSkipped ? [] : (lastList?.items ?? []);
  const rows = principalRows(suggestions, text);
  const note =
    state.status === "failed"
      ? principalSuggestionsNote(null)
      : isSkipped || lastList === null
        ? null
        : principalSuggestionsNote(lastList.sources);
  const isLoading = state.status === "loading";

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
            disabled={disabled}
            data-placeholder={value === null ? "" : undefined}
            className="h-auto min-h-10 min-w-0 flex-1 justify-between py-1.5 font-normal data-[placeholder]:text-muted-foreground"
            {...aria}
          >
            {value === null ? (
              <span className="truncate">
                Search users, groups and service principals
              </span>
            ) : (
              <span className="flex min-w-0 items-center rounded-sm bg-secondary px-2 py-0.5 text-secondary-foreground">
                <PrincipalLabel choice={value} />
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
            label="Who to share with"
            shouldFilter={false}
            loop
            value={highlighted}
            onValueChange={setHighlighted}
          >
            <CommandInput
              placeholder="Type a name, an e-mail, a group or an id"
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
              {!isLoading && rows.length === 0 ? (
                <p className="px-3 py-4 text-sm text-muted-foreground">
                  Type a name, an e-mail address, a group name or a service
                  principal id.
                </p>
              ) : null}
              {rows.map((row) => (
                <CommandItem
                  key={row.id}
                  value={row.id}
                  aria-label={row.text}
                  onSelect={() => {
                    choose(row.choice);
                  }}
                  className="gap-2"
                >
                  {row.isTyped ? (
                    <span className="flex min-w-0 items-center gap-2">
                      <PrincipalIcon type={row.choice.type} />
                      <span className="truncate">
                        Use <span className="font-mono">“{text.trim()}”</span>
                      </span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {PRINCIPAL_TYPE_LABEL[row.choice.type]}
                      </span>
                      <CornerDownLeftIcon
                        aria-hidden="true"
                        className="ml-auto text-muted-foreground"
                      />
                    </span>
                  ) : (
                    <PrincipalLabel choice={row.choice} />
                  )}
                </CommandItem>
              ))}
              {note === null ? null : (
                <p className="border-t px-3 py-2 text-xs text-muted-foreground">
                  {note}
                </p>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {value === null ? null : (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={`Clear ${value.label}`}
          disabled={disabled}
          onClick={clear}
        >
          <XIcon aria-hidden="true" />
        </Button>
      )}
    </div>
  );
}
