"use client";

import { useState } from "react";

import { putAppEgress } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { REASON_UNKNOWN_ACTOR } from "@/lib/app/actions";
import { LiveRegion } from "@/components/live-region";
import { Section } from "@/components/section";
import { EgressEditor } from "@/components/platform/egress-editor";
import { PlatformProvider } from "@/components/platform/platform-context";
import { EgressRulesTable } from "./egress-rules-table";

const REASON_EGRESS_PLATFORM_ADMIN =
  "Only a platform admin changes an app's egress rules. Ask one if the app needs to reach something these rules stop.";

/**
 * The app's own egress rules (design 6.3: set by a platform admin only; the
 * app page shows them read-only to everyone else). A platform admin gets the
 * same editor as on the Platform page; its saves refresh this app.
 */
export function AppEgressSection({
  app,
  actor,
  onReload,
}: {
  app: AppDetail;
  actor: Actor | null;
  onReload: () => void;
}) {
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const isEditor = actor?.isPlatformAdmin === true;

  return (
    <Section title="Egress rules">
      {isEditor ? (
        <PlatformProvider
          value={{ refresh: onReload, announce: setAnnouncement }}
        >
          <LiveRegion message={announcement} />
          <EgressEditor
            name={`Egress rules of ${app.slug}`}
            scope="app"
            source={{ rules: app.egress, version: app.version }}
            save={async (rules, version) => {
              const detail = await putAppEgress(app.slug, {
                version: version ?? app.version,
                rules,
              });
              return { rules: detail.egress, version: detail.version };
            }}
            savedTitle={`Saved the egress rules of ${app.slug}`}
          />
        </PlatformProvider>
      ) : (
        <div className="flex flex-col gap-4">
          <p className="text-muted-foreground">
            The connections {app.slug} opens are checked against these rules
            first, in order; the first rule that matches decides.
          </p>
          <EgressRulesTable
            caption={`Egress rules of ${app.slug}`}
            rules={app.egress}
            empty={
              <p className="text-muted-foreground">
                {app.slug} has no rules of its own, as every new app starts: the
                global rules below decide, and what they do not stop is allowed.
              </p>
            }
          />
          <p className="text-sm text-muted-foreground">
            {actor === null
              ? REASON_UNKNOWN_ACTOR
              : REASON_EGRESS_PLATFORM_ADMIN}
          </p>
        </div>
      )}
    </Section>
  );
}
