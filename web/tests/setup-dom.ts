import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no ResizeObserver, which Recharts' ResponsiveContainer needs. This
// one never reports a size, so a chart keeps the size the test gives it
// (see tests/components/dashboard/chart-sizes.ts).
class ResizeObserverStub implements ResizeObserver {
  observe(): void {
    // Never reports a size.
  }
  unobserve(): void {
    // Nothing was observed.
  }
  disconnect(): void {
    // Nothing was observed.
  }
}
globalThis.ResizeObserver ??= ResizeObserverStub;

// With `globals: false` Testing Library cannot register its own cleanup.
afterEach(cleanup);
