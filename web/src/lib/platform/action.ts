/**
 * The state of one write on the platform page (approve, save, run the
 * self-check): nothing yet, in flight, done with a sentence to show, or failed
 * with the error to show.
 *
 * A write that is in flight ignores a second start, so a double click sends one
 * request; a result that arrives when no write is in flight is dropped, so a
 * late answer cannot overwrite a newer state.
 */
import type { Tone } from "@/lib/status-presentation";

/** What a finished write tells the person. Text only: the page renders it as text. */
export interface ActionOutcome {
  tone: Tone;
  title: string;
  detail?: string;
}

export type ActionState =
  | { status: "idle" }
  | { status: "busy" }
  | { status: "done"; outcome: ActionOutcome }
  | { status: "failed"; error: unknown };

export const IDLE_ACTION: ActionState = { status: "idle" };

export type ActionEvent =
  | { type: "started" }
  | { type: "succeeded"; outcome: ActionOutcome }
  | { type: "failed"; error: unknown }
  | { type: "cleared" };

export function actionReducer(
  state: ActionState,
  event: ActionEvent,
): ActionState {
  switch (event.type) {
    case "started":
      return state.status === "busy" ? state : { status: "busy" };
    case "succeeded":
      return state.status === "busy"
        ? { status: "done", outcome: event.outcome }
        : state;
    case "failed":
      return state.status === "busy"
        ? { status: "failed", error: event.error }
        : state;
    case "cleared":
      return state.status === "busy" ? state : IDLE_ACTION;
  }
}
