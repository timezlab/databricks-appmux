/**
 * Every shadcn primitive of `src/components/ui` that opens, floats or moves
 * focus, on one page, for csp.spec.ts to drive in Chromium under the reference
 * CSP. build.ts prerenders it (as the static export would) and the browser
 * hydrates it, so the page goes through the same server HTML, hydration and
 * client takeover as a real one.
 */
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
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
} from "@/components/ui/command";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toggle } from "@/components/ui/toggle";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useIsClient } from "@/hooks/use-is-client";

import { READY_ATTRIBUTE } from "./ready.ts";

function ReadyFlag() {
  const isClient = useIsClient();
  useEffect(() => {
    if (isClient) document.documentElement.setAttribute(READY_ATTRIBUTE, "");
  }, [isClient]);
  return null;
}

function Sample({ title, children }: { title: string; children: ReactNode }) {
  const id = `sample-${title.toLowerCase().replaceAll(" ", "-")}`;
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <h2 id={id} className="text-lg font-semibold">
        {title}
      </h2>
      <div className="flex flex-wrap items-start gap-3">{children}</div>
    </section>
  );
}

const PRINCIPALS = [
  { value: "data-team", label: "Data team", description: "Group" },
  { value: "ops-team", label: "Ops team", description: "Group" },
];

function ComboboxSample() {
  const [principal, setPrincipal] = useState("data-team");
  return (
    <Combobox
      aria-label="Principal"
      options={PRINCIPALS}
      value={principal}
      onValueChange={setPrincipal}
      placeholder="Choose a principal"
      searchPlaceholder="Search people and groups"
      emptyMessage="Nobody matches."
      allowCustom
    />
  );
}

function SelectSample() {
  const [range, setRange] = useState("1h");
  return (
    <>
      <Select name="range" value={range} onValueChange={setRange}>
        <SelectTrigger aria-label="Range">
          <SelectValue placeholder="Pick a range" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectLabel>Short</SelectLabel>
            <SelectItem value="15m">15 minutes</SelectItem>
            <SelectItem value="1h">1 hour</SelectItem>
          </SelectGroup>
          <SelectSeparator />
          <SelectItem value="24h">24 hours</SelectItem>
          <SelectItem value="7d" disabled>
            7 days
          </SelectItem>
        </SelectContent>
      </Select>
      <Select defaultValue="">
        <SelectTrigger aria-label="Region">
          <SelectValue placeholder="Pick a region" />
        </SelectTrigger>
        <SelectContent
          searchPlaceholder="Search regions"
          emptyMessage="No region matches."
        >
          <SelectItem value="eu-west">Europe (west)</SelectItem>
          <SelectItem value="us-east">US (east)</SelectItem>
          <SelectItem value="ap-south">Asia Pacific (south)</SelectItem>
        </SelectContent>
      </Select>
    </>
  );
}

function TabsSample({ variant }: { variant: "default" | "bar" }) {
  return (
    <Tabs defaultValue="overview">
      <TabsList aria-label={`Sections (${variant})`} variant={variant}>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="logs">Logs</TabsTrigger>
        <TabsTrigger value="environment" disabled>
          Environment
        </TabsTrigger>
      </TabsList>
      <TabsContent value="overview">Overview panel ({variant})</TabsContent>
      <TabsContent value="logs">Logs panel ({variant})</TabsContent>
      <TabsContent value="environment">Environment panel</TabsContent>
    </Tabs>
  );
}

export function PrimitivesPage() {
  const [isWrapped, setIsWrapped] = useState(true);
  const [theme, setTheme] = useState("light");
  const [lastAction, setLastAction] = useState("none");
  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-8 p-4">
      <h1 className="text-2xl font-bold">shadcn primitives under the CSP</h1>
      <p>
        Last menu action: <output aria-label="Last action">{lastAction}</output>
      </p>
      <ReadyFlag />

      <Sample title="Popover">
        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline">Open popover</Button>
          </PopoverTrigger>
          <PopoverContent>
            <PopoverHeader>
              <PopoverTitle>Popover title</PopoverTitle>
              <PopoverDescription>Popover body</PopoverDescription>
            </PopoverHeader>
          </PopoverContent>
        </Popover>
      </Sample>

      <Sample title="Command">
        <Command label="Search apps" className="max-w-sm border">
          <CommandInput placeholder="Search apps" />
          <CommandList>
            <CommandEmpty>No app matches.</CommandEmpty>
            <CommandGroup heading="Apps">
              <CommandItem
                value="sales"
                onSelect={() => {
                  setLastAction("open sales");
                }}
              >
                Sales
              </CommandItem>
              <CommandItem value="finance">Finance</CommandItem>
              <CommandItem value="hr" disabled>
                HR
              </CommandItem>
            </CommandGroup>
          </CommandList>
        </Command>
      </Sample>

      <Sample title="Select">
        <SelectSample />
      </Sample>

      <Sample title="Combobox">
        <ComboboxSample />
      </Sample>

      <Sample title="Dropdown menu">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline">Actions</Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuLabel>sales</DropdownMenuLabel>
            <DropdownMenuItem
              onSelect={() => {
                setLastAction("restart");
              }}
            >
              Restart
            </DropdownMenuItem>
            <DropdownMenuCheckboxItem
              checked={isWrapped}
              onCheckedChange={setIsWrapped}
            >
              Wrap lines
            </DropdownMenuCheckboxItem>
            <DropdownMenuSeparator />
            <DropdownMenuRadioGroup value={theme} onValueChange={setTheme}>
              <DropdownMenuRadioItem value="light">Light</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="dark">Dark</DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>More</DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                <DropdownMenuItem
                  onSelect={() => {
                    setLastAction("copy slug");
                  }}
                >
                  Copy slug
                </DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            <DropdownMenuItem variant="destructive">Delete</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </Sample>

      <Sample title="Tabs">
        <TabsSample variant="default" />
        <TabsSample variant="bar" />
      </Sample>

      <Sample title="Tooltip">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="outline">Copy</Button>
          </TooltipTrigger>
          <TooltipContent>Copy the URL</TooltipContent>
        </Tooltip>
      </Sample>

      <Sample title="Collapsible">
        <Collapsible>
          <CollapsibleTrigger asChild>
            <Button variant="outline">Show details</Button>
          </CollapsibleTrigger>
          <CollapsibleContent>Collapsible details</CollapsibleContent>
        </Collapsible>
      </Sample>

      <Sample title="Toggle">
        <Toggle aria-label="Wrap log lines" variant="outline">
          Wrap
        </Toggle>
        <ToggleGroup type="single" defaultValue="1h" aria-label="Window">
          <ToggleGroupItem value="15m">15 min</ToggleGroupItem>
          <ToggleGroupItem value="1h">1 hour</ToggleGroupItem>
          <ToggleGroupItem value="24h">24 hours</ToggleGroupItem>
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
      </Sample>
    </main>
  );
}
