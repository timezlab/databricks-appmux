"use client";

import { useState } from "react";

import { runSelfCheck } from "@/lib/api";
import type { PlatformView, SelfCheckResult } from "@/lib/api";
import { newerSelfCheck, selfCheckView } from "@/lib/platform/self-check";
import type { SelfCheckRow } from "@/lib/platform/self-check";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import { Notice } from "@/components/notice";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { StatusBadge } from "@/components/status-badge";
import { ActionResult } from "./action-result";
import { useWriteAction } from "./use-write-action";

/**
 * The last `self_check()` result with every check and its state, and a button
 * to run it again. The result of "Run again" is kept here and compared by time
 * with the one the poll sends, so an older poll answer never replaces it.
 */
export function SelfCheckSection({ view }: { view: PlatformView }) {
  const [ranHere, setRanHere] = useState<SelfCheckResult | null>(null);
  const action = useWriteAction();

  const result = newerSelfCheck(view.self_check, ranHere) ?? view.self_check;
  const check = selfCheckView(result);

  function handleRun(): void {
    void action.run(async () => {
      const fresh = await runSelfCheck();
      setRanHere(fresh);
      const outcome = selfCheckView(fresh);
      return {
        tone: outcome.presentation.tone,
        title: `Self-check ${outcome.presentation.label.toLowerCase()}`,
        detail: outcome.summary,
      };
    });
  }

  return (
    <Section
      title="Self-check"
      actions={
        <Button isBusy={action.isBusy} busyLabel="Running" onClick={handleRun}>
          Run again
        </Button>
      }
    >
      <div className="flex flex-col gap-2">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <StatusBadge presentation={check.presentation} />
          <span>{check.summary}.</span>
          <span className="text-muted-foreground">
            Ran <RelativeTime value={check.ranAt} />.
          </span>
        </p>
        {check.state === "failed" ? (
          <Notice
            tone="danger"
            title={`Failed: ${check.failedNames.join(", ")}`}
          >
            <p>A failed check means an isolation guarantee may not hold.</p>
          </Notice>
        ) : null}
        {check.state === "unverified" ? (
          <Notice tone="warning" title="Nothing was verified">
            <p>
              The result lists no checks. Run it again; if it stays empty, treat
              the isolation as unchecked.
            </p>
          </Notice>
        ) : null}
        {/* Twelve green rows say what the summary line says; they open on their own when something is wrong. */}
        <details
          className="text-sm"
          open={check.state === "passed" ? undefined : true}
        >
          <summary className="w-fit cursor-pointer font-medium">
            Show the {check.rows.length} checks
          </summary>
          <div className="mt-2">
            <DataTable<SelfCheckRow>
              caption="Checks of the last self-check"
              isCaptionHidden
              rows={check.rows}
              rowKey={(row) => row.name}
              columns={[
                {
                  key: "name",
                  header: "Check",
                  isRowHeader: true,
                  cell: (row) => (
                    <span className="wrap-break-word">{row.name}</span>
                  ),
                },
                {
                  key: "state",
                  header: "Result",
                  cell: (row) => (
                    <StatusBadge presentation={row.presentation} />
                  ),
                },
                {
                  key: "detail",
                  header: "Detail",
                  cell: (row) => (
                    <span className="wrap-break-word">{row.detail}</span>
                  ),
                },
              ]}
            />
          </div>
        </details>
        <p className="text-muted-foreground">
          A canary child, a real sandbox running a short test program, makes the
          checks with the default rules. They run at startup and after any
          sandbox change; Run again runs them now. Any failed check fails the
          whole result, and a check that cannot run, times out or raises counts
          as failed.
        </p>
        <ActionResult action={action} context="Could not run the self-check" />
      </div>
    </Section>
  );
}
