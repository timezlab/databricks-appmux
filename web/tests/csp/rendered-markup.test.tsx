/**
 * What the components render, checked against the strict CSP the edge serves
 * the admin UI under (FR-021, spike 19: `style-src 'self'`, `script-src 'self'`
 * plus hashes). The HTML is what `renderToStaticMarkup` writes, which is what
 * the static export prerenders; the built files are checked again in
 * tests/export. Nothing in it may hold:
 *  - a `style` attribute (the CSP refuses it);
 *  - a `<style>` element (same);
 *  - a `<script>` element (the pages' own scripts are the layout's, hashed);
 *  - an event-handler attribute (`onclick=...`) or a `javascript:` URL.
 * CSSOM writes after hydration (React's client-side `style` prop) are allowed
 * by the CSP, but the kit does not use them either (ESLint, csp-props test).
 *
 * RULE FOR A NEW SHADCN PRIMITIVE in src/components/ui:
 *  1. Give it a sample in UI_SAMPLES below. This file fails for a primitive
 *     with no sample (and for a sample with no primitive), so a primitive
 *     cannot be added without its server HTML passing the checks above.
 *  2. Do not adopt one that fails them as shipped. The Radix Checkbox, Switch,
 *     RadioGroup, Select, Tabs, ScrollArea and roving-focus groups write an
 *     inline `style` on the server, and so does cmdk's Command (its label);
 *     the modal ones (dialog, sheet, and popover or dropdown-menu when modal)
 *     inject a `<style>` when they open; sidebar, progress, chart and sonner
 *     do one or the other. Use a native element instead (input, meter,
 *     details), write the primitive without the part that styles, or render
 *     that part only once the browser has taken over (`useIsClient`), as
 *     command, tabs and toggle-group here do. Select and Combobox are built on
 *     Popover and Command; Popover and DropdownMenu are never modal.
 *  3. If the browser can open it, give it an opened sample in UI_OPENED too:
 *     what it writes once open is checked in jsdom (no `style` attribute set
 *     through setAttribute, no `<style>` element; CSSOM writes are allowed).
 *  4. Colours come from the tokens in globals.css, never with an opacity
 *     modifier (tests/contract/color-utilities.test.ts).
 *  5. No `style` and no `dangerouslySetInnerHTML` prop (ESLint rule, and the
 *     `@ts-expect-error` lines in csp-props.test.tsx).
 */
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { renderToStaticMarkup, renderToString } from "react-dom/server";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { AppLink } from "@/components/app-link";
import { Button } from "@/components/button";
import { CapacityStrip } from "@/components/capacity-strip";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import { DatabricksMark } from "@/components/databricks-mark";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { LiveRegion } from "@/components/live-region";
import { JumpLinks } from "@/components/jump-links";
import { Loading } from "@/components/loading";
import { MainNav } from "@/components/main-nav";
import { Notice } from "@/components/notice";
import { PageHeader } from "@/components/page-header";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { AppStateBadge, StatusBadge } from "@/components/status-badge";
import { SubNav } from "@/components/sub-nav";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button as UiButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ChartContainer, ChartTooltipContent } from "@/components/ui/chart";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Combobox } from "@/components/ui/combobox";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeCheckbox, NativeRadio } from "@/components/ui/native-choice";
import {
  NativeSelect,
  NativeSelectOptGroup,
  NativeSelectOption,
} from "@/components/ui/native-select";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { Toggle } from "@/components/ui/toggle";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import RootLayout from "@/app/layout";

const COMPONENTS_DIR = path.resolve(
  import.meta.dirname,
  "../../src/components",
);

/** What a CSP-strict page may not contain, as the reasons a piece of HTML fails. */
function cspViolations(html: string): string[] {
  const checks: readonly (readonly [reason: string, pattern: RegExp])[] = [
    ["a style attribute", /\sstyle\s*=/i],
    ["a <style> element", /<style[\s>]/i],
    ["a <script> element", /<script[\s>]/i],
    ["an event-handler attribute", /\son[a-z]+\s*=/i],
    ["a javascript: URL", /javascript:/i],
  ];
  return checks.flatMap(([reason, pattern]) =>
    pattern.test(html) ? [reason] : [],
  );
}

function assertCspClean(element: ReactElement, name: string): void {
  const html = renderToStaticMarkup(element);
  assert.ok(html.length > 0, `${name} renders something`);
  assert.deepEqual(cspViolations(html), [], `${name}: ${html}`);
}

/**
 * What the page writes while a primitive is open in jsdom. jsdom serialises
 * CSSOM writes (`element.style.x = ...`, which the CSP allows) as a `style`
 * attribute, so the live DOM cannot be scanned for one; the writes the CSP
 * refuses are watched instead: `setAttribute("style", ...)` and a `<style>`
 * element anywhere in the document.
 */
interface CspWriteWatch {
  /** The refused writes so far, as reasons. */
  refusedWrites(): string[];
  restore(): void;
}

function watchCspWrites(): CspWriteWatch {
  const setAttribute = vi.spyOn(Element.prototype, "setAttribute");
  const setAttributeNS = vi.spyOn(Element.prototype, "setAttributeNS");
  return {
    refusedWrites() {
      const styleAttributeWrites = [
        ...setAttribute.mock.calls.map(([name]) => name),
        ...setAttributeNS.mock.calls.map(([, name]) => name),
      ].filter((name) => name.toLowerCase() === "style");
      return [
        ...styleAttributeWrites.map(() => "setAttribute('style')"),
        // A `<style>` element (or a script, handler or javascript: URL)
        // anywhere in the document; style attributes are the writes above.
        ...cspViolations(document.documentElement.outerHTML).filter(
          (reason) => reason !== "a style attribute",
        ),
      ];
    },
    restore() {
      setAttribute.mockRestore();
      setAttributeNS.mockRestore();
    },
  };
}

describe("the guard itself", () => {
  it.each([
    ['<p style="color:red">x</p>', ["a style attribute"]],
    ["<style>p{}</style>", ["a <style> element"]],
    ["<script>run()</script>", ["a <script> element"]],
    ['<a onclick="go()">x</a>', ["an event-handler attribute"]],
    ['<a href="javascript:go()">x</a>', ["a javascript: URL"]],
  ])("catches %s", (html, reasons) => {
    expect(cspViolations(html)).toEqual(reasons);
  });

  it("lets ordinary markup through, including words that only look like a hit", () => {
    expect(
      cspViolations(
        '<p class="font-semibold" data-slot="x">Restyle the one-click button</p>',
      ),
    ).toEqual([]);
  });

  it("the write watch catches a style attribute and a <style> element, not a CSSOM write", () => {
    const watch = watchCspWrites();
    const element = document.createElement("p");
    const sheet = document.createElement("style");
    try {
      document.body.append(element);
      element.style.color = "red";
      element.style.setProperty("--x", "1px");
      expect(watch.refusedWrites()).toEqual([]);

      element.setAttribute("style", "color: red");
      document.head.append(sheet);
      expect(watch.refusedWrites()).toEqual([
        "setAttribute('style')",
        "a <style> element",
      ]);
    } finally {
      // A sheet left behind would fail every later check in this file.
      element.remove();
      sheet.remove();
      watch.restore();
    }
  });
});

function CommandSample() {
  return (
    <Command label="Search apps">
      <CommandInput placeholder="Search apps" />
      <CommandList>
        <CommandEmpty>No app matches.</CommandEmpty>
        <CommandGroup heading="Apps">
          <CommandItem value="sales">
            Sales
            <CommandShortcut>S</CommandShortcut>
          </CommandItem>
          <CommandItem value="hr" disabled>
            HR
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
      </CommandList>
    </Command>
  );
}

function SelectSample({ searchPlaceholder }: { searchPlaceholder?: string }) {
  return (
    <>
      <Select name="range" defaultValue="1h">
        <SelectTrigger aria-label="Range" size="sm">
          <SelectValue placeholder="Pick a range" />
        </SelectTrigger>
        <SelectContent
          searchPlaceholder={searchPlaceholder}
          emptyMessage="No range matches."
        >
          <SelectGroup>
            <SelectLabel>Short</SelectLabel>
            <SelectItem value="15m">15 minutes</SelectItem>
            <SelectItem value="1h">1 hour</SelectItem>
          </SelectGroup>
          <SelectSeparator />
          <SelectItem value="7d" disabled>
            7 days
          </SelectItem>
        </SelectContent>
      </Select>
      <Select required>
        <SelectTrigger aria-label="Empty" aria-invalid>
          <SelectValue placeholder="Nothing yet" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">A</SelectItem>
        </SelectContent>
      </Select>
    </>
  );
}

function ComboboxSample() {
  return (
    <Combobox
      aria-label="Principal"
      options={[
        { value: "data-team", label: "Data team", description: "Group" },
        { value: "someone@example.com", label: "Someone" },
      ]}
      value="data-team"
      onValueChange={() => undefined}
      placeholder="Choose a principal"
      searchPlaceholder="Search people and groups"
      emptyMessage="Nobody matches."
      allowCustom
    />
  );
}

function DropdownMenuSample() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UiButton variant="outline">Actions</UiButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>sales</DropdownMenuLabel>
        <DropdownMenuGroup>
          <DropdownMenuItem>
            Restart
            <DropdownMenuShortcut>R</DropdownMenuShortcut>
          </DropdownMenuItem>
          <DropdownMenuCheckboxItem checked>
            Wrap lines
          </DropdownMenuCheckboxItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup value="light">
          <DropdownMenuRadioItem value="light">Light</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">Dark</DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>More</DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuItem>Copy slug</DropdownMenuItem>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        <DropdownMenuItem variant="destructive">Delete</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function TabsSample({ variant }: { variant?: "default" | "bar" }) {
  return (
    <Tabs defaultValue="overview">
      <TabsList aria-label="Sections" variant={variant}>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="logs">Logs</TabsTrigger>
        <TabsTrigger value="environment" disabled>
          Environment
        </TabsTrigger>
      </TabsList>
      <TabsContent value="overview">Overview panel</TabsContent>
      <TabsContent value="logs">Logs panel</TabsContent>
      <TabsContent value="environment">Environment panel</TabsContent>
    </Tabs>
  );
}

function TooltipSample() {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <UiButton variant="outline" size="icon" aria-label="Copy">
            C
          </UiButton>
        </TooltipTrigger>
        <TooltipContent>Copy the URL</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function ToggleGroupSample() {
  return (
    <>
      <ToggleGroup type="single" defaultValue="1h" aria-label="Range">
        <ToggleGroupItem value="15m">15 min</ToggleGroupItem>
        <ToggleGroupItem value="1h">1 hour</ToggleGroupItem>
      </ToggleGroup>
      <ToggleGroup
        type="multiple"
        variant="outline"
        size="sm"
        spacing={2}
        aria-label="Streams"
      >
        <ToggleGroupItem value="stdout">stdout</ToggleGroupItem>
        <ToggleGroupItem value="stderr">stderr</ToggleGroupItem>
      </ToggleGroup>
    </>
  );
}

// One sample per shadcn primitive, in the states that add markup.
const UI_SAMPLES: Record<string, () => ReactElement> = {
  "alert.tsx": () => (
    <Alert variant="danger">
      <AlertTitle>Title</AlertTitle>
      <AlertDescription>Description</AlertDescription>
    </Alert>
  ),
  "badge.tsx": () => (
    <>
      <Badge variant="success">Ready</Badge>
      <Badge asChild variant="info">
        <a href="/x/">Linked</a>
      </Badge>
    </>
  ),
  "button.tsx": () => (
    <>
      <UiButton variant="destructive" size="sm">
        Delete
      </UiButton>
      <UiButton asChild variant="link">
        <a href="/x/">Linked</a>
      </UiButton>
    </>
  ),
  "collapsible.tsx": () => (
    <>
      <Collapsible>
        <CollapsibleTrigger>More</CollapsibleTrigger>
        <CollapsibleContent>Hidden body</CollapsibleContent>
      </Collapsible>
      <Collapsible defaultOpen>
        <CollapsibleTrigger>Less</CollapsibleTrigger>
        <CollapsibleContent>Shown body</CollapsibleContent>
      </Collapsible>
    </>
  ),
  "combobox.tsx": () => <ComboboxSample />,
  "command.tsx": () => <CommandSample />,
  "dropdown-menu.tsx": () => <DropdownMenuSample />,
  "popover.tsx": () => (
    <>
      <Popover>
        <PopoverTrigger>Open</PopoverTrigger>
        <PopoverContent>Body</PopoverContent>
      </Popover>
      <Popover defaultOpen>
        <PopoverAnchor />
        <PopoverTrigger>Opened</PopoverTrigger>
        <PopoverContent>Body</PopoverContent>
      </Popover>
    </>
  ),
  "select.tsx": () => <SelectSample />,
  "tabs.tsx": () => (
    <>
      <TabsSample />
      <TabsSample variant="bar" />
    </>
  ),
  "toggle.tsx": () => (
    <>
      <Toggle aria-label="Wrap lines">W</Toggle>
      <Toggle variant="outline" size="sm" defaultPressed>
        Pause
      </Toggle>
    </>
  ),
  "toggle-group.tsx": () => <ToggleGroupSample />,
  "tooltip.tsx": () => <TooltipSample />,
  // A chart is drawn only after the browser has taken over; the server HTML is
  // the placeholder (and the tooltip card, which is plain text).
  "chart.tsx": () => (
    <>
      <ChartContainer
        label="Requests per minute"
        description="Requests, 09:00 to 10:00."
      >
        <div />
      </ChartContainer>
      <ChartTooltipContent active label={0} tips={["09:00: 4 requests"]} />
    </>
  ),
  "empty.tsx": () => (
    <Empty>
      <EmptyHeader>
        <EmptyMedia variant="icon">!</EmptyMedia>
        <EmptyTitle>Nothing</EmptyTitle>
        <EmptyDescription>Add one.</EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <UiButton>Add</UiButton>
      </EmptyContent>
    </Empty>
  ),
  "input.tsx": () => <Input aria-invalid name="a" defaultValue="x" />,
  "label.tsx": () => <Label htmlFor="a">Label</Label>,
  "native-choice.tsx": () => (
    <>
      <NativeCheckbox name="a" defaultChecked />
      <NativeRadio name="b" value="1" />
    </>
  ),
  "native-select.tsx": () => (
    <NativeSelect name="a" defaultValue="1">
      <NativeSelectOption value="1">One</NativeSelectOption>
      <NativeSelectOptGroup label="More">
        <NativeSelectOption value="2">Two</NativeSelectOption>
      </NativeSelectOptGroup>
    </NativeSelect>
  ),
  "card.tsx": () => (
    <>
      <Card>Panel</Card>
      <Card asChild>
        <article>Article panel</article>
      </Card>
    </>
  ),
  "separator.tsx": () => (
    <>
      <Separator />
      <Separator orientation="vertical" decorative={false} />
    </>
  ),
  "skeleton.tsx": () => <Skeleton />,
  "table.tsx": () => (
    <Table frameLabel="Sample">
      <TableCaption>Caption</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead>Head</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableRow>
          <TableCell>Cell</TableCell>
        </TableRow>
      </TableBody>
      <TableFooter>
        <TableRow>
          <TableCell>Foot</TableCell>
        </TableRow>
      </TableFooter>
    </Table>
  ),
  "textarea.tsx": () => <Textarea aria-invalid name="a" defaultValue="x" />,
};

// How the browser opens each primitive that opens; each step checks that it
// did, so a sample that silently stays closed fails.
const UI_OPENED: Record<
  string,
  { sample: () => ReactElement; open: (user: UserEvent) => Promise<void> }
> = {
  "collapsible.tsx": {
    sample: () => (
      <Collapsible>
        <CollapsibleTrigger>More</CollapsibleTrigger>
        <CollapsibleContent>Hidden body</CollapsibleContent>
      </Collapsible>
    ),
    open: async (user) => {
      await user.click(screen.getByRole("button", { name: "More" }));
      expect(screen.getByText("Hidden body")).toBeVisible();
    },
  },
  "combobox.tsx": {
    sample: () => <ComboboxSample />,
    open: async (user) => {
      await user.click(screen.getByRole("combobox", { name: "Principal" }));
      await user.keyboard("some");
      expect(screen.getByRole("option", { name: /Someone/ })).toBeVisible();
      await user.keyboard("thing");
      expect(
        screen.getByRole("option", { name: "Use “something”" }),
      ).toBeVisible();
    },
  },
  "command.tsx": {
    sample: () => <CommandSample />,
    open: async (user) => {
      await user.click(screen.getByRole("combobox", { name: "Search apps" }));
      await user.keyboard("sa");
      expect(screen.getByRole("option", { name: /Sales/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await user.keyboard("zz");
      expect(screen.getByText("No app matches.")).toBeVisible();
    },
  },
  "dropdown-menu.tsx": {
    sample: () => <DropdownMenuSample />,
    open: async (user) => {
      screen.getByRole("button", { name: "Actions" }).focus();
      await user.keyboard("{Enter}{End}{ArrowUp}{ArrowRight}");
      expect(screen.getByRole("menuitem", { name: "Copy slug" })).toBeVisible();
    },
  },
  "popover.tsx": {
    sample: () => (
      <Popover>
        <PopoverTrigger>Open</PopoverTrigger>
        <PopoverContent>Body</PopoverContent>
      </Popover>
    ),
    open: async (user) => {
      await user.click(screen.getByRole("button", { name: "Open" }));
      expect(screen.getByRole("dialog")).toHaveTextContent("Body");
    },
  },
  "select.tsx": {
    sample: () => <SelectSample searchPlaceholder="Search ranges" />,
    open: async (user) => {
      await user.click(screen.getByRole("combobox", { name: "Range" }));
      await user.keyboard("15{Enter}");
      expect(screen.getByRole("combobox", { name: "Range" })).toHaveTextContent(
        "15 minutes",
      );
      await user.click(screen.getByRole("combobox", { name: "Empty" }));
      await user.keyboard("{ArrowDown}");
      expect(screen.getByRole("listbox")).toBeVisible();
    },
  },
  "tabs.tsx": {
    sample: () => <TabsSample variant="bar" />,
    open: async (user) => {
      await user.tab();
      await user.keyboard("{ArrowRight}");
      expect(screen.getByRole("tabpanel")).toHaveTextContent("Logs panel");
    },
  },
  "toggle-group.tsx": {
    sample: () => <ToggleGroupSample />,
    open: async (user) => {
      await user.tab();
      await user.keyboard("{ArrowLeft} ");
      expect(screen.getByRole("radio", { name: "15 min" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    },
  },
  "tooltip.tsx": {
    sample: () => <TooltipSample />,
    open: async (user) => {
      await user.tab();
      expect(screen.getByRole("tooltip")).toHaveTextContent("Copy the URL");
    },
  },
};

function ConfirmButtonSample() {
  return (
    <ConfirmButton
      label="Delete app"
      prompt="Delete sales?"
      confirmLabel="Yes, delete sales"
      onConfirm={() => undefined}
    />
  );
}

// One sample per component of the shared kit (src/components/*.tsx).
const KIT_SAMPLES: Record<string, () => ReactElement> = {
  "app-link.tsx": () => (
    <>
      <AppLink slug="sales">Sales</AppLink>
      <AppLink slug="sales" buttonVariant="primary" size="small">
        Open
      </AppLink>
      <AppLink slug="../etc">Rejected slug</AppLink>
    </>
  ),
  "button.tsx": () => (
    <>
      <Button variant="primary">Save</Button>
      <Button variant="danger" size="small">
        Delete
      </Button>
      <Button variant="danger-filled" size="small">
        Yes, delete
      </Button>
      <Button variant="ghost">Cancel</Button>
      <Button isBusy busyLabel="Saving">
        Save
      </Button>
    </>
  ),
  "confirm-button.tsx": () => <ConfirmButtonSample />,
  "data-table.tsx": () => (
    <>
      <DataTable
        caption="Apps"
        isCaptionHidden
        columns={[
          {
            key: "slug",
            header: "App",
            cell: (row: { slug: string; memory: number }) => row.slug,
            isRowHeader: true,
          },
          {
            key: "memory",
            header: "Memory",
            cell: (row: { slug: string; memory: number }) => row.memory,
            align: "end",
          },
        ]}
        rows={[{ slug: "sales", memory: 3 }]}
        rowKey={(row) => row.slug}
      />
      <DataTable caption="Empty" columns={[]} rows={[]} rowKey={() => ""} />
    </>
  ),
  "databricks-mark.tsx": () => <DatabricksMark />,
  "empty-state.tsx": () => (
    <EmptyState title="No apps yet" action={<Button>Register</Button>}>
      Register the first one.
    </EmptyState>
  ),
  "error-notice.tsx": () => (
    <ErrorNotice
      error={new Error("boom")}
      context="Could not save"
      onReload={() => undefined}
      onRetry={() => undefined}
    />
  ),
  "field.tsx": () => (
    <>
      <Field label="Slug" hint="Lower case." error="Taken." isRequired>
        {(control) => <Input {...control} name="slug" />}
      </Field>
      <Field label="Public" layout="inline">
        {(control) => <NativeCheckbox {...control} name="public" />}
      </Field>
    </>
  ),
  "form-layout.tsx": () => (
    <form className={FORM_STACK_CLASSES}>
      <FieldGrid>
        <Field label="Slug">{(control) => <Input {...control} />}</Field>
      </FieldGrid>
      <FormActions primary={<Button type="submit">Save</Button>}>
        <Button>Cancel</Button>
      </FormActions>
    </form>
  ),
  "jump-links.tsx": () => (
    <JumpLinks
      label="Groups"
      items={[
        { label: "Traffic limits", href: "#traffic" },
        { label: "Releases", href: "#releases" },
      ]}
    />
  ),
  "live-region.tsx": () => <LiveRegion message="Saved sales" />,
  "loading.tsx": () => <Loading label="Loading apps" />,
  "main-nav.tsx": () => <MainNav />,
  "capacity-strip.tsx": () => (
    <>
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[
          {
            key: "used",
            label: "Used",
            amount: 30,
            fill: "used",
            valueText: "30 MB",
          },
        ]}
        figure="30% used"
      />
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[
          {
            key: "used",
            label: "Used",
            amount: 90,
            fill: "danger",
            valueText: "90 MB",
          },
        ]}
        level="danger"
        levelText="Almost full"
        hasLegend={false}
      />
    </>
  ),
  "notice.tsx": () => (
    <>
      <Notice tone="info" title="Heads up" />
      <Notice tone="danger" title="Failed" live="assertive">
        <p>Detail</p>
      </Notice>
    </>
  ),
  "page-header.tsx": () => (
    <PageHeader
      title="Apps"
      description="All of them."
      actions={<Button>Refresh</Button>}
    />
  ),
  "relative-time.tsx": () => (
    <>
      <RelativeTime value="2026-10-31T01:00:00Z" />
      <RelativeTime value={null} />
    </>
  ),
  "section.tsx": () => (
    <Section title="Releases" actions={<Button>Add</Button>}>
      <p>Body</p>
    </Section>
  ),
  "sub-nav.tsx": () => (
    <SubNav
      label="Sections"
      items={[
        {
          label: "Overview",
          href: "/platform/",
          badge: { text: "3", description: "3 requests waiting" },
        },
        { label: "Apps", href: "/platform/apps/" },
      ]}
    />
  ),
  "status-badge.tsx": () => (
    <>
      <AppStateBadge state="crashed" />
      <StatusBadge
        presentation={{
          label: "Ready",
          tone: "success",
          mark: "✓",
          description: "Serving.",
        }}
      />
    </>
  ),
};

function tsxFilesIn(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".tsx"))
    .map((entry) => entry.name)
    .toSorted();
}

describe("shadcn primitives (src/components/ui)", () => {
  it("every primitive has a sample here, and every sample has a primitive", () => {
    assert.deepEqual(
      tsxFilesIn(path.join(COMPONENTS_DIR, "ui")),
      Object.keys(UI_SAMPLES).toSorted(),
    );
  });

  for (const [file, sample] of Object.entries(UI_SAMPLES)) {
    it(`${file} renders no style, script or handler`, () => {
      assertCspClean(sample(), file);
    });
  }

  it("every opened sample belongs to a primitive", () => {
    for (const file of Object.keys(UI_OPENED)) {
      assert.ok(file in UI_SAMPLES, `${file} has no primitive sample`);
    }
  });

  for (const [file, { sample, open }] of Object.entries(UI_OPENED)) {
    it(`${file}, once open in the browser, writes no style attribute or <style>`, async () => {
      const watch = watchCspWrites();
      try {
        render(sample());
        await open(userEvent.setup());
        assert.deepEqual(watch.refusedWrites(), [], file);
      } finally {
        watch.restore();
      }
    });
  }

  // A primitive that renders other markup once the browser has taken over
  // (`useIsClient`) must still hydrate the prerendered HTML as it is.
  for (const [file, sample] of Object.entries(UI_SAMPLES)) {
    it(`${file} hydrates its server HTML without a mismatch`, () => {
      const container = document.createElement("div");
      container.innerHTML = renderToString(sample());
      document.body.append(container);
      const recoverableErrors: unknown[] = [];
      const consoleError = vi
        .spyOn(console, "error")
        .mockImplementation(() => undefined);
      try {
        render(sample(), {
          container,
          hydrate: true,
          onRecoverableError: (error) => recoverableErrors.push(error),
        });
        assert.deepEqual(recoverableErrors, [], file);
        assert.deepEqual(consoleError.mock.calls, [], file);
      } finally {
        consoleError.mockRestore();
      }
    });
  }
});

describe("the shared kit (src/components)", () => {
  it("every component has a sample here, and every sample has a component", () => {
    assert.deepEqual(
      tsxFilesIn(COMPONENTS_DIR),
      Object.keys(KIT_SAMPLES).toSorted(),
    );
  });

  for (const [file, sample] of Object.entries(KIT_SAMPLES)) {
    it(`${file} renders no style, script or handler`, () => {
      assertCspClean(sample(), file);
    });
  }

  it("the question state of ConfirmButton is clean too", async () => {
    const user = userEvent.setup();
    const { container } = render(
      KIT_SAMPLES["confirm-button.tsx"]?.() ?? <></>,
    );
    await user.click(screen.getByRole("button", { name: "Delete app" }));
    assert.deepEqual(cspViolations(container.innerHTML), []);
  });

  it("the document shell is clean, with the skip link, the header and the noscript notice", () => {
    const html = renderToStaticMarkup(
      <RootLayout>
        <p>Page</p>
      </RootLayout>,
    );
    assert.ok(html.includes('href="#main"') && html.includes("<noscript>"));
    // `<noscript>` holds the markup of its Notice as text in a DOM, so the
    // scan above also covers it.
    assert.deepEqual(cspViolations(html), []);
  });
});
