import Link from "next/link";

import { AppLink } from "@/components/app-link";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { assertNever } from "@/lib/assert-never";

export type AppProblemKind =
  | { kind: "missing" }
  | { kind: "invalid"; value: string }
  | { kind: "not-found"; slug: string }
  | { kind: "no-access"; slug: string };

const BACK_TO_LAUNCHER = <Link href="/">Back to the launcher</Link>;

/**
 * The page when there is no app to show: no slug in the address, a slug that
 * cannot be one, an app that does not exist, or an app the visitor may open
 * but not manage. Every value shown here is text; only `AppLink` makes a link
 * from a slug, and only after checking it.
 */
export function AppProblem(problem: AppProblemKind) {
  switch (problem.kind) {
    case "missing":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title="App" />
          <EmptyState title="No app chosen" action={BACK_TO_LAUNCHER}>
            This address names no app. Choose one on the launcher.
          </EmptyState>
        </div>
      );
    case "invalid":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title="App not found" />
          <EmptyState title="That is not an app name" action={BACK_TO_LAUNCHER}>
            An app name has 1 to 31 lower-case letters, digits or hyphens. The
            address held{" "}
            <span className="font-mono text-[0.9em] wrap-break-word">
              {problem.value}
            </span>
            .
          </EmptyState>
        </div>
      );
    case "not-found":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title="App not found" />
          <EmptyState title="There is no such app" action={BACK_TO_LAUNCHER}>
            No app is registered as{" "}
            <span className="font-mono text-[0.9em] wrap-break-word">
              {problem.slug}
            </span>
            . It may have been deleted, or the name may have been typed wrong.
          </EmptyState>
        </div>
      );
    case "no-access":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title={problem.slug} />
          <EmptyState
            title="You cannot manage this app"
            action={
              <div className="flex flex-wrap items-center gap-2">
                <AppLink slug={problem.slug} buttonVariant="primary">
                  Open the app
                </AppLink>
                {BACK_TO_LAUNCHER}
              </div>
            }
          >
            This page is for the app&apos;s admins. If you may open the app, the
            button below takes you to it; ask an admin of the app for more.
          </EmptyState>
        </div>
      );
    default:
      return assertNever(problem);
  }
}
