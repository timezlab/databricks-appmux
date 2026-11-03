"use client";

import { Command as CommandPrimitive } from "cmdk";
import { cn } from "cn";
import { CheckIcon, ChevronsUpDownIcon, PlusIcon } from "lucide-react";
import { useState } from "react";
import type { ComponentProps, ReactNode } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

// shadcn's combobox recipe (Popover + Command + a trigger button) as one
// component. The popover is not modal and portalled, so nothing of the list
// is prerendered (see popover.tsx and command.tsx for the CSP reasons).
//  - Without `onSearchChange` the options are filtered here, by label,
//    description, keywords and value. With it the caller searches (on the
//    server; debouncing is the caller's) and the options are shown as given.
//  - `allowCustom` offers what was typed as the value ("Use “...”"), for a
//    principal that the directory cannot list, such as a full e-mail.
//  - The search starts empty at each opening (`onSearchChange("")` is called
//    when it is cleared).

interface ComboboxOption {
  value: string;
  label: string;
  /** A second line, such as the kind or the identifier. */
  description?: string;
  /** More words the client-side filter matches. */
  keywords?: readonly string[];
  disabled?: boolean;
}

type ComboboxProps<TOption extends ComboboxOption> = Omit<
  ComponentProps<"button">,
  "value" | "defaultValue" | "onChange" | "children" | "type"
> & {
  options: readonly TOption[];
  value: string;
  /** The value chosen, with its option, or `null` for a custom value. */
  onValueChange: (value: string, option: TOption | null) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyMessage?: ReactNode;
  /** Called with the search text as it changes; turns the local filter off. */
  onSearchChange?: (search: string) => void;
  loading?: boolean;
  loadingMessage?: string;
  /** Draws an option's row; the default is the label over the description. */
  renderOption?: (option: TOption) => ReactNode;
  allowCustom?: boolean;
  contentClassName?: string;
};

function DefaultOptionRow({ option }: { option: ComboboxOption }) {
  return (
    <span className="flex min-w-0 flex-col">
      <span className="truncate">{option.label}</span>
      {option.description === undefined ? null : (
        <span className="truncate text-xs text-muted-foreground">
          {option.description}
        </span>
      )}
    </span>
  );
}

function Combobox<TOption extends ComboboxOption>({
  options,
  value,
  onValueChange,
  placeholder = "Choose…",
  searchPlaceholder = "Search…",
  emptyMessage = "No results.",
  onSearchChange,
  loading = false,
  loadingMessage = "Loading…",
  renderOption,
  allowCustom = false,
  className,
  contentClassName,
  ...triggerProps
}: ComboboxProps<TOption>) {
  const [isOpen, setIsOpen] = useState(false);
  const [search, setSearch] = useState("");
  // The label of the last pick, for when the options no longer hold it (a
  // server search moved on, or the value was typed).
  const [picked, setPicked] = useState<{ value: string; label: string }>();

  function changeSearch(next: string) {
    setSearch(next);
    onSearchChange?.(next);
  }

  function changeOpen(next: boolean) {
    setIsOpen(next);
    if (!next && search !== "") changeSearch("");
  }

  function choose(nextValue: string, option: TOption | null) {
    setPicked({ value: nextValue, label: option?.label ?? nextValue });
    onValueChange(nextValue, option);
    changeOpen(false);
  }

  const chosenLabel =
    value === ""
      ? undefined
      : (options.find((option) => option.value === value)?.label ??
        (picked?.value === value ? picked.label : value));
  const customValue = search.trim();
  const isCustomOffered =
    allowCustom &&
    customValue !== "" &&
    !options.some((option) => option.value === customValue);

  return (
    <Popover open={isOpen} onOpenChange={changeOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={isOpen}
          data-slot="combobox-trigger"
          data-placeholder={chosenLabel === undefined ? "" : undefined}
          className={cn(
            "w-full max-w-xl justify-between font-normal data-[placeholder]:text-muted-foreground",
            className,
          )}
          {...triggerProps}
        >
          <span className="truncate">{chosenLabel ?? placeholder}</span>
          <ChevronsUpDownIcon
            className="text-muted-foreground"
            aria-hidden="true"
          />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className={cn(
          "w-(--radix-popover-trigger-width) min-w-64 p-0",
          contentClassName,
        )}
      >
        <Command
          label={searchPlaceholder}
          shouldFilter={onSearchChange === undefined}
          defaultValue={value === "" ? undefined : value}
        >
          <CommandInput
            placeholder={searchPlaceholder}
            value={search}
            onValueChange={changeSearch}
          />
          <CommandList>
            {loading ? (
              <CommandPrimitive.Loading
                label={loadingMessage}
                className="py-6 text-center text-sm text-muted-foreground"
              >
                {loadingMessage}
              </CommandPrimitive.Loading>
            ) : null}
            {loading || isCustomOffered ? null : (
              <CommandEmpty>{emptyMessage}</CommandEmpty>
            )}
            {options.map((option) => (
              <CommandItem
                key={option.value}
                value={option.value}
                keywords={[
                  option.label,
                  ...(option.description === undefined
                    ? []
                    : [option.description]),
                  ...(option.keywords ?? []),
                ]}
                disabled={option.disabled}
                onSelect={() => {
                  choose(option.value, option);
                }}
              >
                {renderOption === undefined ? (
                  <DefaultOptionRow option={option} />
                ) : (
                  renderOption(option)
                )}
                <CheckIcon
                  className={cn(
                    "ml-auto",
                    option.value === value ? undefined : "invisible",
                  )}
                  aria-hidden="true"
                />
              </CommandItem>
            ))}
            {isCustomOffered ? (
              <CommandItem
                forceMount
                value={customValue}
                onSelect={() => {
                  choose(customValue, null);
                }}
              >
                <PlusIcon aria-hidden="true" />
                Use “{customValue}”
              </CommandItem>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export { Combobox };
export type { ComboboxOption, ComboboxProps };
