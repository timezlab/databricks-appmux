"use client";

import { useRef, useState } from "react";

import type { PlatformApp, PlatformView } from "@/lib/api";
import { formatMegabytes, pluralize } from "@/lib/format";
import type { ActionOutcome } from "@/lib/platform/action";
import { filterApps } from "@/lib/platform/apps";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { Field } from "@/components/field";
import { Loading } from "@/components/loading";
import { Notice } from "@/components/notice";
import { Section } from "@/components/section";
import { Input } from "@/components/ui/input";
import { AppPanel } from "./app-panel";

function delegationText(app: PlatformApp): string {
  const kinds = [
    app.obo_delegated ? "OBO" : null,
    app.shared_sp_delegated ? "Shared SP" : null,
  ].filter((kind) => kind !== null);
  return kinds.length === 0 ? "none" : kinds.join(", ");
}

/** Every registered app with its platform fields; "Edit" opens one app's forms below the table. */
export function AppsSection({ view }: { view: PlatformView }) {
  const [query, setQuery] = useState("");
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  // After a rename the panel follows the app to its new slug, which the list
  // does not hold until the next fetch; the outcome stays here because the
  // form that produced it is replaced.
  const [renamed, setRenamed] = useState<{
    to: string;
    outcome: ActionOutcome;
  } | null>(null);
  const filterRef = useRef<HTMLInputElement>(null);

  const apps = filterApps(view.apps, query);
  const selected =
    selectedSlug === null
      ? null
      : (view.apps.find((app) => app.slug === selectedSlug) ?? null);

  function closePanel(): void {
    setSelectedSlug(null);
    setRenamed(null);
    filterRef.current?.focus();
  }

  return (
    <Section title="Apps">
      <div className="flex flex-col gap-4">
        <Field label="Filter apps" hint="By slug, display name or owner.">
          {(control) => (
            <Input
              {...control}
              ref={filterRef}
              type="search"
              autoComplete="off"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
              }}
            />
          )}
        </Field>
        <DataTable<PlatformApp>
          caption={`Registered apps (${pluralize(apps.length, "app")} shown of ${view.apps.length})`}
          rows={apps}
          rowKey={(app) => app.slug}
          empty={
            <EmptyState
              title={
                view.apps.length === 0
                  ? "No apps registered"
                  : "No app matches the filter"
              }
            >
              {view.apps.length === 0
                ? "Register one below, or approve a request in the inbox."
                : "Clear the filter to see every app."}
            </EmptyState>
          }
          columns={[
            {
              key: "slug",
              header: "Slug",
              isRowHeader: true,
              cell: (app) => (
                <span className="font-mono text-[0.9em] wrap-break-word">
                  {app.slug}
                </span>
              ),
            },
            {
              key: "name",
              header: "Name",
              cell: (app) => (
                <span className="wrap-break-word">{app.display_name}</span>
              ),
            },
            {
              key: "owner",
              header: "Owner",
              cell: (app) => (
                <span className="wrap-break-word">{app.owner}</span>
              ),
            },
            {
              key: "memory",
              header: "Memory budget",
              align: "end",
              cell: (app) => formatMegabytes(app.memory_budget_mb),
            },
            {
              key: "delegations",
              header: "Delegations",
              cell: delegationText,
            },
            {
              key: "egress",
              header: "Egress rules",
              align: "end",
              cell: (app) => app.egress.length,
            },
            {
              key: "actions",
              header: "Actions",
              isLabelHiddenWhenStacked: true,
              cell: (app) => (
                <Button
                  size="small"
                  aria-label={`Edit ${app.slug}`}
                  aria-expanded={app.slug === selectedSlug}
                  onClick={() => {
                    setSelectedSlug(app.slug);
                  }}
                >
                  Edit
                </Button>
              ),
            },
          ]}
        />
        {renamed === null ? null : (
          <Notice tone={renamed.outcome.tone} title={renamed.outcome.title}>
            {renamed.outcome.detail === undefined ? null : (
              <p>{renamed.outcome.detail}</p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="small"
                onClick={() => {
                  setRenamed(null);
                }}
              >
                Dismiss
              </Button>
            </div>
          </Notice>
        )}
        {selectedSlug !== null && selected === null ? (
          renamed?.to === selectedSlug ? (
            <Loading label="Loading the renamed app" />
          ) : (
            <Notice tone="warning" title="That app is no longer in the list">
              <p>It was renamed or removed since you opened it.</p>
              <div className="flex flex-wrap items-center gap-2">
                <Button size="small" onClick={closePanel}>
                  Close
                </Button>
              </div>
            </Notice>
          )
        ) : null}
        {selected === null ? null : (
          <AppPanel
            key={selected.slug}
            app={selected}
            otherSlugs={view.apps
              .filter((app) => app.slug !== selected.slug)
              .map((app) => app.slug)}
            onRenamed={(_from, to, outcome) => {
              setSelectedSlug(to);
              setRenamed({ to, outcome });
            }}
            onClose={closePanel}
          />
        )}
      </div>
    </Section>
  );
}
