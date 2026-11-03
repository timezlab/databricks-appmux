"use client";

import { Command as CommandPrimitive } from "cmdk";
import { cn } from "cn";
import { CheckIcon, ChevronDownIcon } from "lucide-react";
import { Popover as PopoverPrimitive } from "radix-ui";
import {
  Children,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type {
  ComponentProps,
  KeyboardEvent,
  ReactNode,
  RefObject,
} from "react";

import { CommandInput } from "@/components/ui/command";

// shadcn's select (new-york-v4) with its names, props and classes, built on
// Popover and cmdk's Command instead of Radix Select. Radix Select writes
// inline `style` attributes on the server (its hidden native select, the
// value) and its content locks scrolling with an injected `<style>`; the CSP
// (style-src 'self') refuses both. Here:
//  - the popover is not modal and renders through a Portal, so nothing of it
//    is prerendered; floating-ui places it with CSSOM writes, which the CSP
//    allows;
//  - the keyboard is a listbox's: ArrowDown/ArrowUp, Enter or Space open it
//    on the chosen item; arrows, Home and End move; Enter or Space picks;
//    typing jumps to the item that starts with the letters (type-ahead), or,
//    with `searchPlaceholder`, filters in a search box; Escape and Tab close;
//    focus goes back to the trigger;
//  - `name` adds a hidden input, so a plain form submits the value;
//  - the scroll buttons are left out (the list scrolls natively).
// An item's value may not be "", which means "nothing chosen" (as in Radix).

/** How long the letters typed for type-ahead count as one word. */
const TYPE_AHEAD_RESET_MS = 1000;

interface SelectContextValue {
  value: string;
  open: boolean;
  disabled: boolean;
  required: boolean;
  /** Item labels by value, for the trigger while the list is closed. */
  labels: ReadonlyMap<string, string>;
  setOpen: (open: boolean) => void;
  pick: (value: string) => void;
  registerLabel: (value: string, label: string) => () => void;
}

const SelectContext = createContext<SelectContextValue | null>(null);

/**
 * True for the copy of the items `SelectContent` always renders: they render
 * nothing and only tell the select their labels, so the trigger can show the
 * chosen item's label while the list (in the portal) is closed.
 */
const LabelCollectionContext = createContext(false);

function useSelectContext(part: string): SelectContextValue {
  const context = useContext(SelectContext);
  if (context === null) {
    throw new Error(`<${part}> must be inside <Select>.`);
  }
  return context;
}

/** The text of a React node, for an item's label and type-ahead. */
function textOf(node: ReactNode): string {
  return Children.toArray(node)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") {
        return String(child);
      }
      if (isValidElement<{ children?: ReactNode }>(child)) {
        return textOf(child.props.children);
      }
      return "";
    })
    .join("");
}

interface SelectProps {
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** The form field name: adds a hidden input that holds the value. */
  name?: string;
  form?: string;
  disabled?: boolean;
  required?: boolean;
  children?: ReactNode;
}

function Select({
  value: valueProp,
  defaultValue = "",
  onValueChange,
  open: openProp,
  defaultOpen = false,
  onOpenChange,
  name,
  form,
  disabled = false,
  required = false,
  children,
}: SelectProps) {
  const [uncontrolledValue, setUncontrolledValue] = useState(defaultValue);
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const [labels, setLabels] = useState<ReadonlyMap<string, string>>(
    () => new Map(),
  );
  const value = valueProp ?? uncontrolledValue;
  const open = !disabled && (openProp ?? uncontrolledOpen);

  const setOpen = useCallback(
    (next: boolean) => {
      setUncontrolledOpen(next);
      onOpenChange?.(next);
    },
    [onOpenChange],
  );

  const registerLabel = useCallback((itemValue: string, label: string) => {
    setLabels((previous) =>
      previous.get(itemValue) === label
        ? previous
        : new Map(previous).set(itemValue, label),
    );
    return () => {
      setLabels((previous) => {
        if (!previous.has(itemValue)) return previous;
        const next = new Map(previous);
        next.delete(itemValue);
        return next;
      });
    };
  }, []);

  const context: SelectContextValue = {
    value,
    open,
    disabled,
    required,
    labels,
    setOpen,
    pick: (next) => {
      setUncontrolledValue(next);
      setOpen(false);
      if (next !== value) onValueChange?.(next);
    },
    registerLabel,
  };

  return (
    <SelectContext value={context}>
      <PopoverPrimitive.Root open={open} onOpenChange={setOpen} modal={false}>
        {children}
      </PopoverPrimitive.Root>
      {name === undefined ? null : (
        <input
          type="hidden"
          name={name}
          value={value}
          form={form}
          disabled={disabled}
        />
      )}
    </SelectContext>
  );
}

function SelectTrigger({
  className,
  size = "default",
  children,
  onKeyDown,
  ...props
}: ComponentProps<typeof PopoverPrimitive.Trigger> & {
  size?: "sm" | "default";
}) {
  const context = useSelectContext("SelectTrigger");
  return (
    <PopoverPrimitive.Trigger
      data-slot="select-trigger"
      data-size={size}
      data-placeholder={context.value === "" ? "" : undefined}
      role="combobox"
      aria-haspopup="listbox"
      aria-required={context.required || undefined}
      disabled={context.disabled}
      className={cn(
        "flex w-fit items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 py-2 text-sm whitespace-nowrap shadow-xs transition-colors motion-reduce:transition-none hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-2 aria-invalid:border-destructive data-[placeholder]:text-muted-foreground data-[size=default]:h-10 data-[size=sm]:h-8 *:data-[slot=select-value]:line-clamp-1 *:data-[slot=select-value]:flex *:data-[slot=select-value]:items-center *:data-[slot=select-value]:gap-2 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4 [&_svg:not([class*='text-'])]:text-muted-foreground",
        className,
      )}
      {...props}
      onKeyDown={(event) => {
        onKeyDown?.(event);
        if (event.defaultPrevented) return;
        // Enter and Space open it as a button press; the arrows too, as on a
        // native select.
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          context.setOpen(true);
        }
      }}
    >
      {children}
      <ChevronDownIcon className="size-4" aria-hidden="true" />
    </PopoverPrimitive.Trigger>
  );
}

function SelectValue({
  className,
  placeholder,
  children,
  ...props
}: Omit<ComponentProps<"span">, "placeholder"> & { placeholder?: ReactNode }) {
  const context = useSelectContext("SelectValue");
  const shown =
    context.value === ""
      ? placeholder
      : (children ?? context.labels.get(context.value));
  return (
    <span
      data-slot="select-value"
      className={cn("pointer-events-none", className)}
      {...props}
    >
      {shown}
    </span>
  );
}

function SelectContent({
  className,
  children,
  align = "start",
  sideOffset = 4,
  searchPlaceholder,
  emptyMessage = "No results.",
  label,
  ...props
}: ComponentProps<typeof PopoverPrimitive.Content> & {
  /** Adds a search box that filters the items, with this placeholder. */
  searchPlaceholder?: string;
  /** Shown when the search matches nothing. */
  emptyMessage?: ReactNode;
  /** The listbox's accessible name. */
  label?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  return (
    <>
      <LabelCollectionContext value={true}>{children}</LabelCollectionContext>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          data-slot="select-content"
          role={undefined}
          align={align}
          sideOffset={sideOffset}
          className={cn(
            "relative z-50 flex max-h-(--radix-popover-content-available-height) min-w-[max(8rem,var(--radix-popover-trigger-width))] origin-(--radix-popover-content-transform-origin) flex-col overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md data-[state=closed]:animate-menu-out data-[state=open]:animate-menu-in",
            className,
          )}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            (inputRef.current ?? listRef.current)?.focus();
          }}
          {...props}
        >
          <SelectList
            searchPlaceholder={searchPlaceholder}
            emptyMessage={emptyMessage}
            label={label}
            inputRef={inputRef}
            listRef={listRef}
          >
            {children}
          </SelectList>
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </>
  );
}

/** The open list. It mounts on each opening, so the highlight starts on the chosen item. */
function SelectList({
  searchPlaceholder,
  emptyMessage,
  label,
  inputRef,
  listRef,
  children,
}: {
  searchPlaceholder: string | undefined;
  emptyMessage: ReactNode;
  label: string | undefined;
  inputRef: RefObject<HTMLInputElement | null>;
  listRef: RefObject<HTMLDivElement | null>;
  children: ReactNode;
}) {
  const context = useSelectContext("SelectContent");
  const [highlighted, setHighlighted] = useState(context.value);
  const typed = useRef({ text: "", at: Number.NEGATIVE_INFINITY });
  const isSearchable = searchPlaceholder !== undefined;

  function jumpTo(letter: string, at: number) {
    const state = typed.current;
    state.text =
      at - state.at > TYPE_AHEAD_RESET_MS ? letter : state.text + letter;
    state.at = at;
    const search = state.text.toLowerCase();
    const items = [
      ...(listRef.current?.querySelectorAll<HTMLElement>(
        '[cmdk-item]:not([aria-disabled="true"])',
      ) ?? []),
    ];
    const valueOf = (item: HTMLElement) => item.dataset.value ?? "";
    const labelOf = (item: HTMLElement) =>
      (context.labels.get(valueOf(item)) ?? item.textContent).toLowerCase();
    // The same letter again steps through the items that start with it.
    const isOneLetterRepeated = [...search].every((char) => char === search[0]);
    const current = items.findIndex((item) => valueOf(item) === highlighted);
    const candidates = isOneLetterRepeated
      ? [...items.slice(current + 1), ...items.slice(0, current + 1)]
      : items;
    const prefix = isOneLetterRepeated ? search.slice(0, 1) : search;
    const match = candidates.find((item) => labelOf(item).startsWith(prefix));
    if (match !== undefined) setHighlighted(valueOf(match));
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Tab") {
      event.preventDefault();
      context.setOpen(false);
      return;
    }
    if (isSearchable) return; // the search box takes the letters and spaces
    if (event.key === " ") {
      event.preventDefault();
      if (highlighted !== "") context.pick(highlighted);
      return;
    }
    const isLetter =
      event.key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey;
    if (isLetter) {
      event.preventDefault();
      jumpTo(event.key, event.timeStamp);
    }
  }

  return (
    <CommandPrimitive
      data-slot="select-command"
      className="flex min-h-0 flex-col"
      label={searchPlaceholder ?? label}
      value={highlighted}
      onValueChange={setHighlighted}
      onKeyDown={handleKeyDown}
    >
      {isSearchable ? (
        <CommandInput ref={inputRef} placeholder={searchPlaceholder} />
      ) : null}
      <CommandPrimitive.List
        ref={listRef}
        label={label}
        data-slot="select-list"
        className="max-h-[300px] scroll-py-1 overflow-x-hidden overflow-y-auto p-1 outline-hidden"
      >
        {isSearchable ? (
          <CommandPrimitive.Empty
            data-slot="select-empty"
            className="py-6 text-center text-sm"
          >
            {emptyMessage}
          </CommandPrimitive.Empty>
        ) : null}
        {children}
      </CommandPrimitive.List>
    </CommandPrimitive>
  );
}

function SelectGroup({
  className,
  children,
  ...props
}: ComponentProps<typeof CommandPrimitive.Group>) {
  const isCollecting = useContext(LabelCollectionContext);
  if (isCollecting) return <>{children}</>;
  return (
    <CommandPrimitive.Group
      data-slot="select-group"
      className={cn("text-foreground", className)}
      {...props}
    >
      {children}
    </CommandPrimitive.Group>
  );
}

function SelectLabel({ className, ...props }: ComponentProps<"div">) {
  const isCollecting = useContext(LabelCollectionContext);
  if (isCollecting) return null;
  return (
    <div
      data-slot="select-label"
      className={cn("px-2 py-1.5 text-xs text-muted-foreground", className)}
      {...props}
    />
  );
}

function SelectItem({
  className,
  children,
  value,
  textValue,
  ...props
}: Omit<
  ComponentProps<typeof CommandPrimitive.Item>,
  "value" | "onSelect" | "keywords"
> & {
  value: string;
  /** The text for the trigger and type-ahead, when the children are not plain text. */
  textValue?: string;
}) {
  const context = useSelectContext("SelectItem");
  const isCollecting = useContext(LabelCollectionContext);
  const label = textValue ?? textOf(children);
  const { registerLabel } = context;

  useLayoutEffect(() => {
    if (isCollecting) return registerLabel(value, label);
  }, [isCollecting, registerLabel, value, label]);

  if (isCollecting) return null;
  const isChosen = context.value === value;
  return (
    <CommandPrimitive.Item
      data-slot="select-item"
      data-state={isChosen ? "checked" : "unchecked"}
      value={value}
      keywords={[label]}
      onSelect={() => {
        context.pick(value);
      }}
      className={cn(
        "relative flex w-full cursor-default items-center gap-2 rounded-sm py-1.5 pr-8 pl-2 text-sm outline-hidden select-none data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-50 data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4 [&_svg:not([class*='text-'])]:text-muted-foreground *:[span]:last:flex *:[span]:last:items-center *:[span]:last:gap-2",
        className,
      )}
      {...props}
    >
      <span
        data-slot="select-item-indicator"
        className="absolute right-2 flex size-3.5 items-center justify-center"
      >
        {isChosen ? <CheckIcon className="size-4" aria-hidden="true" /> : null}
      </span>
      <span data-slot="select-item-text">{children}</span>
    </CommandPrimitive.Item>
  );
}

function SelectSeparator({
  className,
  ...props
}: ComponentProps<typeof CommandPrimitive.Separator>) {
  const isCollecting = useContext(LabelCollectionContext);
  if (isCollecting) return null;
  return (
    <CommandPrimitive.Separator
      data-slot="select-separator"
      className={cn("pointer-events-none -mx-1 my-1 h-px bg-border", className)}
      {...props}
    />
  );
}

export {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
};
