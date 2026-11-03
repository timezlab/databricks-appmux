"use client";

import type { ReactNode } from "react";

import { getPlatform } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import {
  ALWAYS_ALLOWED_RANGE,
  SHIPPED_GLOBAL_EGRESS,
  appEgressMode,
} from "@/lib/app/egress";
import type { AppEgressMode } from "@/lib/app/egress";
import { assertNever } from "@/lib/assert-never";
import { pluralize } from "@/lib/format";
import { pollData } from "@/lib/poll/data";
import { usePoll } from "@/hooks/use-poll";
import { ErrorNotice } from "@/components/error-notice";
import { Section } from "@/components/section";
import { SkeletonTable } from "@/components/skeletons";
import { EgressRulesTable } from "./egress-rules-table";

// The global rules change rarely, and only on the Platform page.
const PLATFORM_POLL_INTERVAL_MS = 60_000;

/** What the app's own list means for the steps after it. */
function modeWords(slug: string, mode: AppEgressMode): string {
  switch (mode.kind) {
    case "pass-through":
      return `${slug} has no rules of its own (as every new app starts), so the next step decides.`;
    case "allow-list":
      return `${slug} has an allow-list: ${pluralize(mode.allowed, "allow rule")} and then a deny of every address. Anything the allow rules do not name is stopped here, so the global rules are never reached.`;
    case "closed":
      return `${slug}'s own rules deny every address, so nothing past its own network is reachable and the global rules are never reached.`;
    case "rules":
      return `${slug} has ${pluralize(mode.count, "rule")} of its own (above). A connection none of them matches goes on to the next step.`;
    default:
      return assertNever(mode);
  }
}

function Step({ title, children }: { title: string; children: ReactNode }) {
  return (
    <li className="flex flex-col gap-2 border-l-2 pl-4">
      <h3 className="text-base font-semibold">{title}</h3>
      {children}
    </li>
  );
}

/**
 * Where the app's connections may go, in the order appmux decides (design
 * 6.3): its virtual network, its own rules, the global rules, then default
 * allow; and what no rule can open. A platform admin sees the global rules as
 * stored; anyone else may not read the platform's settings, so they see the
 * list appmux ships, marked as such.
 */
export function ReachSection({
  app,
  actor,
}: {
  app: AppDetail;
  actor: Actor | null;
}) {
  const isPlatformAdmin = actor?.isPlatformAdmin === true;
  const platform = usePoll((signal) => getPlatform({ signal }), {
    intervalMs: PLATFORM_POLL_INTERVAL_MS,
    enabled: isPlatformAdmin,
  });
  const mode = appEgressMode(app.egress);
  const isGlobalReached = mode.kind === "pass-through" || mode.kind === "rules";
  const globalRules = pollData(platform)?.egress_global ?? null;

  return (
    <Section title="What the app may reach">
      <p className="text-muted-foreground">
        Each connection {app.slug} opens goes through appmux, which decides in
        this order. The first rule that matches decides.
      </p>
      <ol className="flex flex-col gap-6">
        <Step
          title={`1. Its own network, ${ALWAYS_ALLOWED_RANGE}: always allowed`}
        >
          <p>
            The far end of the app&apos;s virtual network cable, and its DNS
            resolver at 10.0.2.3. No rule can stop it, or name resolution would
            stop too.
          </p>
        </Step>
        <Step title="2. The app's own rules">
          <p>{modeWords(app.slug, mode)}</p>
        </Step>
        <Step title="3. The global rules">
          {isGlobalReached ? null : (
            <p className="text-muted-foreground">
              Not reached for {app.slug}; shown for reference.
            </p>
          )}
          {!isPlatformAdmin ? (
            <>
              <p>
                Set by a platform admin for every app. As appmux ships them they
                are these two; a platform admin may have changed them since.
              </p>
              <EgressRulesTable
                caption="The global rules as appmux ships them"
                rules={SHIPPED_GLOBAL_EGRESS}
                empty={null}
              />
            </>
          ) : globalRules !== null ? (
            <EgressRulesTable
              caption="The global rules, as stored"
              rules={globalRules}
              empty={
                <p className="text-muted-foreground">
                  The global list is empty: nothing is stopped at this step.
                  Change it under Platform, Egress.
                </p>
              }
            />
          ) : platform.status === "failed" ? (
            <ErrorNotice
              error={platform.error}
              context="Could not read the global rules"
              onRetry={platform.refresh}
            />
          ) : (
            <div>
              <p className="sr-only" role="status">
                Loading the global rules
              </p>
              <SkeletonTable rows={2} columns={5} />
            </div>
          )}
        </Step>
        <Step title="4. Anything else: allowed">
          <p>
            A connection no rule matched goes through. That is why an allow-list
            ends with a deny of every address.
          </p>
        </Step>
      </ol>
      <div className="flex flex-col gap-2">
        <h3 className="text-base font-semibold">
          Never reachable, whatever the rules say
        </h3>
        <ul className="list-disc pl-5">
          <li>
            The container&apos;s loopback: appmux starts the app&apos;s network
            with it blocked, so 10.0.2.2 does not lead to appmux or anything
            else listening on the container&apos;s 127.0.0.1.
          </li>
          <li>
            Other apps&apos; sockets: each app has its own network namespace.
          </li>
          <li>
            IPv6: appmux drops it, so an IPv6 connection fails at once
            (ENETUNREACH).
          </li>
        </ul>
      </div>
    </Section>
  );
}
