/**
 * Shared by the tests of the app page's forms and sections: the contract's
 * example app, the real `useAppActions` hook around a component, and a
 * `fetch` stand-in that records the calls, so a test sees what a form sends
 * (route and body) rather than a mock of the API module.
 */
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { vi } from "vitest";

import { useAppActions } from "@/components/app/use-app-actions";
import type { AppActions } from "@/components/app/use-app-actions";
import { API_BASE_URL, AppDetail } from "@/lib/api";
import { stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";
import { readExample } from "../../support/contract-examples";

export { jsonResponse } from "../../support/fake-fetch";
export { readExample };

/** The contract's `AppDetail` example: app `sales`, owned by alice, with two grants and six resources. */
export const APP: AppDetail = AppDetail.parse(readExample("AppDetail"));

export function appWith(overrides: Partial<AppDetail>): AppDetail {
  return { ...APP, ...overrides };
}

/** Renders what `children` builds from the page's real write controller. */
export function renderWithActions(
  children: (actions: AppActions) => ReactNode,
) {
  const onChanged = vi.fn();
  const onNotice = vi.fn();
  function Host() {
    const actions = useAppActions({ onChanged, onNotice });
    return <>{children(actions)}</>;
  }
  return { ...render(<Host />), onChanged, onNotice };
}

/** Answers every API call with `respond()` and returns the list the calls are recorded in. */
export function stubApi(
  respond: () => Response | Promise<Response>,
): RecordedCall[] {
  const calls: RecordedCall[] = [];
  stubFetch((call) => {
    calls.push(call);
    return respond();
  });
  return calls;
}

/** What a call sent: its method, path under the API base and parsed JSON body. */
export function describeCall(call: RecordedCall | undefined): {
  method: string | undefined;
  path: string;
  body: unknown;
} {
  if (call === undefined) throw new Error("The API was not called.");
  const { body } = call.init;
  return {
    method: call.init.method,
    path: call.url.replace(API_BASE_URL, ""),
    body: typeof body === "string" ? (JSON.parse(body) as unknown) : undefined,
  };
}
