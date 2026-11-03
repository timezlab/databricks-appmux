"use client";

import { KeyRoundIcon } from "lucide-react";
import { useId, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import { LiveRegion } from "@/components/live-region";
import { Section } from "@/components/section";
import { SkeletonLines, SkeletonTable } from "@/components/skeletons";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { usePoll } from "@/hooks/use-poll";
import { getAppEnvironment } from "@/lib/api";
import type { AppEnvironment } from "@/lib/api";
import {
  PACKAGE_SOURCE_FILTERS,
  environmentJson,
  filterPackages,
  filterVariables,
  packagesCopyText,
  runtimeRows,
  variableSourceText,
  variableValueText,
  variablesCopyText,
} from "@/lib/app/environment";
import type { PackageSourceFilter } from "@/lib/app/environment";
import { pluralize } from "@/lib/format";
import { pollData } from "@/lib/poll/data";
import { AppSectionLink } from "../app-section-link";

/** The environment changes only with a release; a slow poll keeps it current without noise. */
const ENVIRONMENT_POLL_INTERVAL_MS = 30_000;

type EnvironmentView = "structured" | "json";
type Variable = AppEnvironment["variables"][number];
type Package = AppEnvironment["packages"][number];

const PACKAGE_SOURCE_LABELS: Record<PackageSourceFilter, string> = {
  all: "All",
  release: "Release",
  wheel: "Wheel",
  platform: "Platform",
};

function isPackageSourceFilter(value: string): value is PackageSourceFilter {
  return (PACKAGE_SOURCE_FILTERS as readonly string[]).includes(value);
}

export interface EnvironmentPanelProps {
  slug: string;
}

/**
 * The Environment section: the runtime versions, the variables and the
 * packages the app's child starts with, as tables with a search, or as the
 * JSON the server sent. A secret shows as `***` everywhere: the server never
 * sends its value, and `lib/app/environment.ts` masks it once more.
 */
export function EnvironmentPanel({ slug }: EnvironmentPanelProps) {
  const [view, setView] = useState<EnvironmentView>("structured");
  const poll = usePoll((signal) => getAppEnvironment(slug, { signal }), {
    intervalMs: ENVIRONMENT_POLL_INTERVAL_MS,
    resetKey: `environment:${slug}`,
  });
  const environment = pollData(poll);

  if (environment === null) {
    return (
      <Section title="Environment">
        {poll.status === "failed" ? (
          <ErrorNotice
            error={poll.error}
            context="Could not load the environment"
            onRetry={poll.refresh}
          />
        ) : (
          <>
            <p role="status" className="sr-only">
              Loading the environment
            </p>
            <SkeletonLines lines={2} />
            <SkeletonTable rows={4} columns={3} />
          </>
        )}
      </Section>
    );
  }

  if (environment.release === null) {
    return (
      <Section title="Environment">
        <EmptyState
          title="No release yet"
          action={
            <AppSectionLink
              slug={slug}
              section="deployments"
              buttonVariant="secondary"
            >
              Go to Deployments
            </AppSectionLink>
          }
        >
          The environment is read from the active release. Press Update in
          Deployments to make the first one.
        </EmptyState>
      </Section>
    );
  }

  return (
    <Section
      title="Environment"
      actions={
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          aria-label="Show as"
          value={view}
          onValueChange={(value) => {
            // A single group sends "" when its pressed item is pressed again; keep the choice.
            if (value === "structured" || value === "json") setView(value);
          }}
        >
          <ToggleGroupItem value="structured">Structured</ToggleGroupItem>
          <ToggleGroupItem value="json">JSON</ToggleGroupItem>
        </ToggleGroup>
      }
    >
      {poll.status === "stale" ? (
        <ErrorNotice
          error={poll.error}
          context="Could not refresh the environment"
          onRetry={poll.refresh}
        />
      ) : null}
      {view === "json" ? (
        <JsonView environment={environment} />
      ) : (
        <StructuredView environment={environment} />
      )}
    </Section>
  );
}

function JsonView({ environment }: { environment: AppEnvironment }) {
  const id = useId();
  const text = environmentJson(environment);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          The answer of the environment route, as the server sent it. Secrets
          are null.
        </p>
        <CopyButton value={text} label="the JSON" selectTargetId={id} />
      </div>
      <pre
        id={id}
        tabIndex={0}
        aria-label="Environment as JSON"
        className="max-h-[min(32rem,70vh)] overflow-auto rounded-control border bg-card p-3 font-mono text-xs leading-5 whitespace-pre-wrap wrap-anywhere"
      >
        {text}
      </pre>
    </div>
  );
}

function StructuredView({ environment }: { environment: AppEnvironment }) {
  return (
    <div className="flex flex-col gap-6">
      <section
        aria-labelledby="environment-runtime"
        className="flex flex-col gap-2"
      >
        <h3 id="environment-runtime">Runtime</h3>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Release</dt>
          <dd>{environment.release}</dd>
          {runtimeRows(environment.runtime).map((row) => (
            <div key={row.label} className="contents">
              <dt className="text-muted-foreground">{row.label}</dt>
              <dd className="font-mono">{row.value}</dd>
            </div>
          ))}
        </dl>
      </section>
      <Tabs defaultValue="variables">
        <TabsList variant="bar" aria-label="What the app starts with">
          <TabsTrigger value="variables">
            Variables ({environment.variables.length})
          </TabsTrigger>
          <TabsTrigger value="packages">
            Packages ({environment.packages.length})
          </TabsTrigger>
        </TabsList>
        <TabsContent value="variables" className="pt-4">
          <VariablesTab variables={environment.variables} />
        </TabsContent>
        <TabsContent value="packages" className="pt-4">
          <PackagesTab packages={environment.packages} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

const VARIABLE_COLUMNS: DataTableColumn<Variable>[] = [
  {
    key: "name",
    header: "Name",
    isRowHeader: true,
    cell: (variable) => (
      <span className="font-mono wrap-anywhere">{variable.name}</span>
    ),
  },
  {
    key: "value",
    header: "Value",
    cell: (variable) => {
      const text = variableValueText(variable);
      if (variable.is_secret) {
        return (
          <span className="inline-flex flex-wrap items-center gap-2">
            <span className="font-mono">{text}</span>
            <Badge variant="neutral">
              <KeyRoundIcon aria-hidden="true" />
              secret
            </Badge>
          </span>
        );
      }
      return variable.value === null ? (
        <span className="text-muted-foreground">{text}</span>
      ) : (
        <span className="font-mono wrap-anywhere">{text}</span>
      );
    },
  },
  {
    key: "source",
    header: "Source",
    cell: (variable) => variableSourceText(variable),
  },
];

const PACKAGE_COLUMNS: DataTableColumn<Package>[] = [
  {
    key: "name",
    header: "Name",
    isRowHeader: true,
    cell: (item) => (
      <span className="font-mono wrap-anywhere">{item.name}</span>
    ),
  },
  {
    key: "version",
    header: "Version",
    cell: (item) =>
      item.version === null ? (
        <span className="text-muted-foreground">not stated</span>
      ) : (
        <span className="font-mono wrap-anywhere">{item.version}</span>
      ),
  },
  {
    key: "source",
    header: "Source",
    cell: (item) => item.source,
  },
];

function SearchField({
  label,
  query,
  shown,
  total,
  noun,
  onChange,
}: {
  label: string;
  query: string;
  shown: number;
  total: number;
  noun: string;
  onChange: (query: string) => void;
}) {
  const hasQuery = query.trim() !== "";
  return (
    <div className="w-full sm:max-w-xs">
      <Field
        label={label}
        hint={
          hasQuery
            ? `${String(shown)} of ${pluralize(total, noun)} match.`
            : undefined
        }
      >
        {(control) => (
          <Input
            {...control}
            type="search"
            value={query}
            autoComplete="off"
            onChange={(event) => {
              onChange(event.target.value);
            }}
          />
        )}
      </Field>
      <LiveRegion
        message={hasQuery ? `${pluralize(shown, noun)} found` : null}
      />
    </div>
  );
}

function VariablesTab({ variables }: { variables: readonly Variable[] }) {
  const [query, setQuery] = useState("");
  const shown = filterVariables(variables, query);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <SearchField
          label="Search variables"
          query={query}
          shown={shown.length}
          total={variables.length}
          noun="variable"
          onChange={setQuery}
        />
        <CopyButton
          value={variablesCopyText(shown)}
          label="the shown variables"
        />
      </div>
      <DataTable
        caption="Variables"
        isCaptionHidden
        columns={VARIABLE_COLUMNS}
        rows={shown}
        rowKey={(variable) => variable.name}
        empty={
          <EmptyState
            title={
              variables.length === 0
                ? "No variables"
                : "No variable matches the search"
            }
          >
            {variables.length === 0
              ? "The release sets no variables and appmux adds none."
              : "Clear the search to see every variable."}
          </EmptyState>
        }
      />
    </div>
  );
}

function PackagesTab({ packages }: { packages: readonly Package[] }) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<PackageSourceFilter>("all");
  const shown = filterPackages(packages, query, source);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
          <SearchField
            label="Search packages"
            query={query}
            shown={shown.length}
            total={packages.length}
            noun="package"
            onChange={setQuery}
          />
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            aria-label="Source"
            value={source}
            onValueChange={(value) => {
              // A single group sends "" when its pressed item is pressed again; keep the choice.
              if (isPackageSourceFilter(value)) setSource(value);
            }}
          >
            {PACKAGE_SOURCE_FILTERS.map((item) => (
              <ToggleGroupItem key={item} value={item}>
                {PACKAGE_SOURCE_LABELS[item]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <CopyButton
          value={packagesCopyText(shown)}
          label="the shown packages"
        />
      </div>
      <DataTable
        caption="Packages"
        isCaptionHidden
        columns={PACKAGE_COLUMNS}
        rows={shown}
        rowKey={(item) => `${item.source}:${item.name}`}
        empty={
          <EmptyState
            title={
              packages.length === 0
                ? "No packages"
                : "No package matches the search"
            }
          >
            {packages.length === 0
              ? "The release declares no packages."
              : "Clear the search or choose All to see every package."}
          </EmptyState>
        }
      />
    </div>
  );
}
