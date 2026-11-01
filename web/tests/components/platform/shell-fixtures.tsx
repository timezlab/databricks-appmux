/**
 * What the tests of the platform page's shell, apps and settings share: the
 * contract's `PlatformView` example (parsed, so it satisfies the schema), and a
 * render that wraps a section in the page's context with spies for the two
 * things a write asks of the page (`refresh` and `announce`).
 */
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { PlatformProvider } from "@/components/platform/platform-context";
import { AppDetail, PlatformView } from "@/lib/api/schemas";
import { readExample } from "../../support/contract-examples";

/** A fresh copy of the contract's `PlatformView` example: three apps, `sales` first (version 3). */
export function platformView(): PlatformView {
  return PlatformView.parse(readExample("PlatformView"));
}

/** The contract's `AppDetail` example (slug `sales`), with `changes` applied: the answer of a successful write. */
export function appDetail(changes: Partial<AppDetail> = {}): AppDetail {
  return { ...AppDetail.parse(readExample("AppDetail")), ...changes };
}

export function renderInPlatform(ui: ReactElement) {
  const refresh = vi.fn();
  const announce = vi.fn();
  const wrap = (node: ReactElement) => (
    <PlatformProvider value={{ refresh, announce }}>{node}</PlatformProvider>
  );
  const result = render(wrap(ui));
  return {
    ...result,
    refresh,
    announce,
    /** Renders `next` in the same provider, as the page does when a poll brings new data. */
    rerenderInPlatform: (next: ReactElement) => {
      result.rerender(wrap(next));
    },
  };
}
