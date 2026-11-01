/**
 * The sentence shown after an action on the app page succeeds. Each says what
 * was asked and what happens next, and nothing the call has not yet done: a
 * start or an activation is answered before it finishes, and the poll shows
 * the result.
 */
import type { ReleaseOutcome, RequestRow } from "@/lib/api";
import { assertNever } from "@/lib/assert-never";
import { requestKindLabel } from "./requests";

export function updateOutcomeMessage(outcome: ReleaseOutcome): string {
  if (outcome.status === "up_to_date") {
    return `Already up to date: the source folder has the same files as release ${outcome.release_no}, so no new release was created.`;
  }
  return `Release ${outcome.release_no} was created and is being built. The current release keeps serving until it is healthy.`;
}

export type AppControlAction = "start" | "stop" | "restart";

const APP_CONTROL_MESSAGE: Record<AppControlAction, string> = {
  start:
    "Asked the app to start. The route opens once its health check passes.",
  stop: "Asked the app to stop.",
  restart:
    "Asked the app to restart. The route opens again once its health check passes.",
};

export function appControlMessage(action: AppControlAction): string {
  return APP_CONTROL_MESSAGE[action];
}

export type ReleaseMessageAction =
  "activate" | "lock" | "unlock" | "delete" | "edit";

export function releaseActionMessage(
  action: ReleaseMessageAction,
  releaseNo: number,
): string {
  switch (action) {
    case "activate":
      return `Asked to activate release ${releaseNo}. The current release keeps serving until it is healthy.`;
    case "lock":
      return `Release ${releaseNo} is locked.`;
    case "unlock":
      return `Release ${releaseNo} is unlocked.`;
    case "delete":
      return `Release ${releaseNo} was deleted.`;
    case "edit":
      return `Saved the name and tags of release ${releaseNo}.`;
    default:
      return assertNever(action);
  }
}

// ---------------------------------------------------------------- resources

export function resourceActionMessage(
  action: "attach" | "detach",
  key: string,
): string {
  const past = action === "attach" ? "attached" : "detached";
  return `Resource ${key} was ${past}. Resources apply at the app's next start; restart the app to apply this change.`;
}

// ------------------------------------------------------------------- grants

export function grantActionMessage(
  action: "add" | "change" | "remove",
  name: string,
): string {
  switch (action) {
    case "add":
      return `${name} was added to the people who can open this app.`;
    case "change":
      return `The level of ${name} was changed.`;
    case "remove":
      return `${name} was removed from the people who can open this app.`;
    default:
      return assertNever(action);
  }
}

// ----------------------------------------------------------------- requests

export function requestSentMessage(kind: RequestRow["kind"]): string {
  return `Sent your ${requestKindLabel(kind).toLowerCase()} request. A platform admin will answer it here.`;
}

export function requestWithdrawnMessage(kind: RequestRow["kind"]): string {
  return `Your ${requestKindLabel(kind).toLowerCase()} request was withdrawn.`;
}
