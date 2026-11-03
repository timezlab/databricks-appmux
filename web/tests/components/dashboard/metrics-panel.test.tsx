import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { MetricsPanel } from "@/components/dashboard/metrics-panel";
import type { MetricsPanelProps } from "@/components/dashboard/metrics-panel";
import { ApiError, getMetrics } from "@/lib/api";
import type { MetricsQuery, MetricsResponse } from "@/lib/api";
import { TIME_RANGES } from "@/lib/dashboard/scope";
import { giveChartsASize } from "./chart-sizes";
import { contractMetrics, metricsResponse, platformRow } from "./fixtures";
import { fakeMatchMedia } from "./match-media";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  getMetrics: vi.fn(),
}));

giveChartsASize();
fakeMatchMedia();

const [, LAST_HOUR, LAST_6_HOURS, LAST_DAY, , LAST_WEEK] = TIME_RANGES;
const MINUTE_MS = 60_000;

beforeEach(() => {
  vi.mocked(getMetrics).mockReset();
});

type PanelOverrides = Partial<MetricsPanelProps>;

const defaultProps: MetricsPanelProps = {
  scope: { kind: "platform" },
  range: LAST_HOUR,
  onRangeChange: () => undefined,
};

function renderPanel(overrides: PanelOverrides = {}) {
  const props = { ...defaultProps, ...overrides };
  const view = render(<MetricsPanel {...props} />);
  return {
    ...view,
    update: (next: PanelOverrides) => {
      view.rerender(<MetricsPanel {...props} {...next} />);
    },
  };
}

/**
 * The window of the range ends at the start of the current minute; the window
 * before ends a whole range earlier. So a query that ends within the last
 * minute is the one of the range.
 */
function isCurrentWindow(query: MetricsQuery | undefined): boolean {
  return Date.now() - Date.parse(query?.to ?? "") < MINUTE_MS;
}

/** Answers the window of the range with `current` and the window before with `previous`. */
function answer(
  current: () => Promise<MetricsResponse>,
  previous: () => Promise<MetricsResponse> = current,
): void {
  vi.mocked(getMetrics).mockImplementation((query) =>
    isCurrentWindow(query) ? current() : previous(),
  );
}

function resolved(response: MetricsResponse) {
  return () => Promise.resolve(response);
}

function rejected(error: Error) {
  return () => Promise.reject(error);
}

/** The contract example with the platform's requests halved: the window before, against which 412 is twice as many. */
function halfAsBusy(): MetricsResponse {
  const example = contractMetrics();
  return {
    ...example,
    items: example.items.map((row) =>
      row.app_slug === "_platform" ? { ...row, requests: 206 } : row,
    ),
  };
}

/** The figure with this label, as a scope to read its value and detail from. */
function figure(label: string) {
  const term = screen.getByText(label, { selector: "dt" });
  const box = term.closest("div");
  if (box === null) throw new Error(`no figure for ${label}`);
  return within(box);
}

function queries(): (MetricsQuery | undefined)[] {
  return vi.mocked(getMetrics).mock.calls.map(([query]) => query);
}

function windowMinutes(query: MetricsQuery | undefined): number {
  return (
    (Date.parse(query?.to ?? "") - Date.parse(query?.from ?? "")) / MINUTE_MS
  );
}

describe("MetricsPanel figures", () => {
  it("shows the platform's figures from the answer, as text in tabular digits", async () => {
    answer(resolved(contractMetrics()));
    renderPanel();

    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
    expect(screen.getByText(/Updated/).closest("p")).toHaveTextContent(
      "2026-10-30 09:15 UTC to 2026-10-30 09:16 UTC. Updated",
    );
    expect(figure("Requests").getByText("412")).toHaveClass("tabular-nums");
    expect(figure("Server errors").getByText("3")).toBeInTheDocument();
    expect(
      figure("Server errors").getByText("0.7% of requests"),
    ).toBeInTheDocument();
    expect(figure("Response time, p50").getByText("≤ 25")).toBeInTheDocument();
    expect(figure("Response time, p95").getByText("≤ 500")).toBeInTheDocument();
    expect(figure("Memory peak").getByText("1,500")).toBeInTheDocument();
    expect(
      figure("Memory peak").getByText("24% of the 6,144 MB limit"),
    ).toBeInTheDocument();
    expect(
      figure("Egress drops").getByText("nothing was blocked"),
    ).toBeInTheDocument();
  });

  it("compares each figure with the window before it, in words", async () => {
    answer(resolved(contractMetrics()), resolved(halfAsBusy()));
    renderPanel();
    await screen.findByText(/Updated/);

    expect(
      figure("Requests").getByText("100% more than the previous hour"),
    ).toBeInTheDocument();
    expect(
      figure("Server errors").getByText("Same as the previous hour"),
    ).toBeInTheDocument();
  });

  it("still shows the window when the window before cannot be read, without the comparison", async () => {
    answer(
      resolved(contractMetrics()),
      rejected(new ApiError(503, "unavailable", "starting up")),
    );
    renderPanel();
    await screen.findByText(/Updated/);

    expect(figure("Requests").getByText("412")).toBeInTheDocument();
    expect(screen.queryByText(/previous hour/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("MetricsPanel charts", () => {
  it("draws each chart in a frame of its own, and says so when one has nothing to show", async () => {
    answer(resolved(contractMetrics()));
    renderPanel();
    await screen.findByText(/Updated/);

    for (const title of [
      "Requests per minute",
      "Response time of requests",
      "Memory peak per minute",
      "Apps running",
    ]) {
      const frame = screen.getByRole("group", { name: title });
      expect(
        within(frame).getByRole("heading", { level: 3, name: title }),
      ).toBeInTheDocument();
      expect(
        within(frame).getByRole("img", { name: title }),
      ).toBeInTheDocument();
    }
    const egress = screen.getByRole("group", {
      name: "Egress drops per minute",
    });
    expect(
      within(egress).getByText("No packet was dropped in this window."),
    ).toBeInTheDocument();
    expect(within(egress).queryByRole("img")).toBeNull();
  });

  it("draws no apps-running chart for one app", async () => {
    answer(resolved(contractMetrics()));
    renderPanel({ scope: { kind: "app", slug: "sales" } });
    await screen.findByText(/Updated/);

    expect(
      screen.getByRole("group", { name: "Requests per minute" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Apps running" })).toBeNull();
    expect(screen.queryByText("Apps running", { selector: "dt" })).toBeNull();
  });

  it("warns, as text, that a long answer was cut", async () => {
    answer(resolved({ ...metricsResponse([platformRow(0)]), truncated: true }));
    renderPanel();
    expect(
      await screen.findByText("The answer was cut to its newest rows"),
    ).toBeInTheDocument();
  });
});

describe("MetricsPanel with no traffic", () => {
  it("names the range and offers the next longer one", async () => {
    answer(resolved(metricsResponse([])));
    const onRangeChange = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onRangeChange });

    expect(
      await screen.findByText("No traffic in the last hour"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Show the last 6 hours" }),
    );
    expect(onRangeChange).toHaveBeenCalledExactlyOnceWith(LAST_6_HOURS.id);
  });

  it("offers no longer range after the longest", async () => {
    answer(resolved(metricsResponse([])));
    renderPanel({ range: LAST_WEEK });

    expect(
      await screen.findByText("No traffic in the last 7 days"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Show the/ })).toBeNull();
  });
});

describe("MetricsPanel polling", () => {
  it("asks for the scope's rows over the range at a step of a minute, then for the hour before, with the abort signal", async () => {
    answer(resolved(contractMetrics()));
    renderPanel({ scope: { kind: "app", slug: "sales" }, range: LAST_HOUR });
    await screen.findByText(/Updated/);

    const [current, previous] = queries();
    expect(current).toMatchObject({ app: "sales", step: "1m" });
    expect(windowMinutes(current)).toBe(LAST_HOUR.minutes);
    expect(previous).toMatchObject({ app: "sales", step: "1m" });
    expect(previous?.to).toBe(current?.from);
    expect(windowMinutes(previous)).toBe(LAST_HOUR.minutes);
    for (const [, options] of vi.mocked(getMetrics).mock.calls) {
      expect(options?.signal).toBeInstanceOf(AbortSignal);
    }
  });

  it("reads 7 days at 15 minutes, and no week before it: those minutes are no longer kept", async () => {
    answer(resolved(contractMetrics()));
    renderPanel({ range: LAST_WEEK });
    await screen.findByText(/Updated/);

    expect(queries()).toHaveLength(1);
    expect(queries()[0]).toMatchObject({ app: "_platform", step: "15m" });
    expect(windowMinutes(queries()[0])).toBe(LAST_WEEK.minutes);
  });

  it("drops the old charts at once when the scope changes, keeping each frame's place, and asks for the new one", async () => {
    answer(resolved(contractMetrics()));
    const panel = renderPanel({ scope: { kind: "platform" } });
    await screen.findByText(/Updated/);
    answer(() => new Promise(() => undefined));

    panel.update({ scope: { kind: "app", slug: "sales" } });

    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading the metrics of the last hour…",
    );
    expect(screen.queryByRole("img")).toBeNull();
    const loading = screen.getByRole("group", { name: "Requests" });
    expect(loading).toHaveAttribute("aria-busy", "true");
    await waitFor(() => {
      expect(queries().at(-1)?.app).toBe("sales");
    });
  });

  it("asks again over the new window when the range changes", async () => {
    answer(resolved(contractMetrics()));
    const panel = renderPanel({ range: LAST_HOUR });
    await screen.findByText(/Updated/);
    vi.mocked(getMetrics).mockClear();

    panel.update({ range: LAST_DAY });

    await waitFor(() => {
      expect(getMetrics).toHaveBeenCalledTimes(2);
    });
    expect(windowMinutes(queries()[0])).toBe(LAST_DAY.minutes);
    expect(queries()[0]?.step).toBe("1m");
  });
});

describe("MetricsPanel failures", () => {
  it("says the metrics could not be loaded when the first call fails, and Try again asks once more", async () => {
    answer(rejected(new ApiError(503, "unavailable", "starting up")));
    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the metrics",
    );
    expect(screen.queryByRole("img")).toBeNull();
    expect(
      within(screen.getByRole("group", { name: "Requests" })).getByText(
        "Not shown: the metrics could not be read.",
      ),
    ).toBeInTheDocument();

    answer(resolved(contractMetrics()));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("explains a range the server will not cover in words of its own", async () => {
    answer(
      rejected(new ApiError(400, "window_too_long", "use step=15m or more")),
    );
    renderPanel({ range: LAST_WEEK });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not load the metrics: the range is longer than the server answers",
    );
    expect(alert).toHaveTextContent("at most 7 days at a time");
    expect(alert).not.toHaveTextContent("The change was not accepted");
  });

  it("keeps the last charts under a warning when a refresh fails after a good answer", async () => {
    answer(resolved(contractMetrics()));
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText(/Updated/);
    answer(rejected(new ApiError(503, "unavailable", "starting up")));

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not refresh the metrics, showing the last ones received",
    );
    expect(figure("Requests").getByText("412")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Requests per minute" }),
    ).toBeInTheDocument();
  });
});
