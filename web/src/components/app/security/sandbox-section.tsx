import type { ReactNode } from "react";

import { Section } from "@/components/section";

interface SandboxFact {
  term: string;
  detail: ReactNode;
}

// Each sentence is a fact of docs/product/v1-design.md 6.1 to 6.4 and 5.7;
// change them together with the design, never ahead of it.
const FACTS: readonly SandboxFact[] = [
  {
    term: "Other apps and appmux",
    detail:
      "The app runs in a sandbox of its own. It cannot see the processes of other apps or of appmux, read their environment or signal them, and it cannot reach their sockets, run directories or releases.",
  },
  {
    term: "Files it can read",
    detail: (
      <>
        Only what it needs, read-only: <Path>/usr</Path>, its packages, its
        release at <Path>/srv/app</Path> and ten files of <Path>/etc</Path>. The
        platform&apos;s files do not exist for it (no <Path>/databricks</Path>,{" "}
        <Path>/sys</Path> or <Path>/mnt</Path>), and <Path>/proc</Path> shows
        only <Path>cpuinfo</Path>, <Path>meminfo</Path> and its own process.
      </>
    ),
  },
  {
    term: "Files it can write",
    detail: (
      <>
        <Path>/tmp</Path> (64 MB), <Path>/var/tmp</Path> (16 MB),{" "}
        <Path>/dev/shm</Path> (64 MB), <Path>/home/app</Path> (64 MB), and its
        run directory <Path>/run/appmux</Path>, which has no size cap. Nothing
        else.
      </>
    ),
  },
  {
    term: "Privileges",
    detail:
      "Every capability is dropped and no new ones can be gained: it cannot mount a filesystem, create a network interface or open a raw socket. It cannot create a user namespace of its own either.",
  },
  {
    term: "Network",
    detail:
      "Its only way out is through appmux, where the egress rules above decide.",
  },
  {
    term: "Processes and CPU",
    detail:
      "The number of processes it may start is capped (the platform setting max_processes_per_child). While the platform setting child_cpu_pin is on, as it is by default, all apps share one CPU, so a busy app cannot stall appmux; apps on that CPU still compete with each other.",
  },
  {
    term: "Memory",
    detail:
      "Its memory budget is watched, not a hard cap: appmux stops the app when it uses more than its budget, and allocations well past it fail. A very fast allocation can still use up the container's memory before the watch notices.",
  },
];

function Path({ children }: { children: string }) {
  return <code className="font-mono text-[0.9em]">{children}</code>;
}

/**
 * What the sandbox forbids (design 6.1 to 6.4, 5.7), in words. The same for
 * every app: appmux refuses to run any app when its self-check finds the
 * isolation open, so the list holds for an app that runs.
 */
export function SandboxSection() {
  return (
    <Section title="What the sandbox forbids">
      <p className="text-muted-foreground">
        The same for every app. appmux checks the isolation when it starts and
        after every change to the sandbox; if any check finds a way out, no app
        runs.
      </p>
      <dl className="flex max-w-180 flex-col gap-4">
        {FACTS.map((fact) => (
          <div key={fact.term} className="flex flex-col gap-1">
            <dt className="font-semibold">{fact.term}</dt>
            <dd className="wrap-break-word text-muted-foreground">
              {fact.detail}
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}
