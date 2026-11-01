import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { MetricsPanel } from "@/components/dashboard/metrics-panel";
import type { MetricsPanelProps } from "@/components/dashboard/metrics-panel";
import { ApiError, getMetrics } from "@/lib/api";
import { TIME_RANGES } from "@/lib/dashboard/scope";
import { giveChartsASize } from "./chart-sizes";
import { contractMetrics, metricsResponse, platformRow } from "./fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  getMetrics: vi.fn(),
}));

giveChartsASize();

const [, LAST_HOUR, , LAST_DAY] = TIME_RANGES;

beforeEach(() => {
  vi.mocked(getMetrics).mockReset();
});

type PanelOverrides = Partial<MetricsPanelProps>;

const defaultProps: MetricsPanelProps = {
  scope: { kind: "platform" },
  range: LAST_HOUR,
  isSelectable: () => true,
  onSelectApp: () => undefined,
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

/** The tile with this label, as a scope to read its value and detail from. */
function tile(label: string) {
  const term = screen.getByText(label, { selector: "dt" });
  const box = term.closest("div");
  if (box === null) throw new Error(`no tile for ${label}`);
  return within(box);
}

function queryOf(call: number) {
  const [query, options] = vi.mocked(getMetrics).mock.calls[call] ?? [];
  return { query, options };
}

describe("MetricsPanel numbers", () => {
  it("shows the platform's headline figures from the answer, as text", async () => {
    vi.mocked(getMetrics).mockResolvedValue(contractMetrics());
    renderPanel();

    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
    expect(screen.getByText(/Updated/).closest("p")).toHaveTextContent(
      "2026-10-30 09:15 UTC to 2026-10-30 09:16 UTC. Updated",
    );
    expect(tile("Requests").getByText("412")).toBeInTheDocument();
    expect(tile("Server errors").getByText("3")).toBeInTheDocument();
    expect(
      tile("Server errors").getByText("0.7% of requests"),
    ).toBeInTheDocument();
    expect(tile("Latency p50").getByText("at most 25 ms")).toBeInTheDocument();
    expect(tile("Latency p95").getByText("at most 500 ms")).toBeInTheDocument();
    expect(tile("Memory peak").getByText("1,500 MB")).toBeInTheDocument();
    expect(
      tile("Memory peak").getByText("24% of the 6,144 MB limit"),
    ).toBeInTheDocument();
    expect(
      tile("Egress drops").getByText("nothing was blocked"),
    ).toBeInTheDocument();
  });

  it("draws the four charts, each in a section of its own, and says so when one has nothing to show", async () => {
    vi.mocked(getMetrics).mockResolvedValue(contractMetrics());
    renderPanel();
    await screen.findByText(/Updated/);

    for (const title of [
      "Requests per minute",
      "Response time of requests",
      "Memory peak per minute",
    ]) {
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
      expect(screen.getByRole("img", { name: title })).toBeInTheDocument();
    }
    expect(
      screen.getByRole("heading", { name: "Egress drops per minute" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No packet was dropped in this window."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("img", { name: "Egress drops per minute" }),
    ).toBeNull();
  });

  it("compares the apps in a table of text and offers a button that opens one app's charts", async () => {
    vi.mocked(getMetrics).mockResolvedValue(contractMetrics());
    const onSelectApp = vi.fn();
    const user = userEvent.setup();
    renderPanel({ scope: { kind: "apps" }, onSelectApp });

    const table = await screen.findByRole("table", {
      name: "Apps compared over the window",
    });
    const row = within(table).getByRole("row", { name: /sales/ });
    expect(
      within(row)
        .getAllByRole("cell")
        .slice(0, 5)
        .map((cell) => cell.textContent),
    ).toEqual(["412", "3", "at most 25 ms", "at most 500 ms", "341 MB"]);
    expect(within(table).getAllByRole("row")).toHaveLength(2);

    await user.click(
      within(row).getByRole("button", { name: /^View charts\s*for sales$/ }),
    );
    expect(onSelectApp).toHaveBeenCalledExactlyOnceWith("sales");
  });

  it("offers no button for an app the person cannot open (deleted since the rows were written)", async () => {
    vi.mocked(getMetrics).mockResolvedValue(contractMetrics());
    renderPanel({ scope: { kind: "apps" }, isSelectable: () => false });
    await screen.findByRole("table", { name: "Apps compared over the window" });
    expect(screen.queryByRole("button", { name: /View charts/ })).toBeNull();
  });

  it("says there is nothing to chart when the window has no rows", async () => {
    vi.mocked(getMetrics).mockResolvedValue(metricsResponse([]));
    renderPanel();
    expect(
      await screen.findByText("No metrics in this window"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("warns, as text, that a long answer was cut", async () => {
    vi.mocked(getMetrics).mockResolvedValue({
      ...metricsResponse([platformRow(0)]),
      truncated: true,
    });
    renderPanel();
    expect(
      await screen.findByText("The answer was cut to its newest rows"),
    ).toBeInTheDocument();
  });
});

describe("MetricsPanel polling", () => {
  it("asks for the scope's rows over the range, with the abort signal so leaving the page cancels it", async () => {
    vi.mocked(getMetrics).mockResolvedValue(contractMetrics());
    renderPanel({ scope: { kind: "app", slug: "sales" }, range: LAST_HOUR });
    await screen.findByText(/Updated/);

    const { query, options } = queryOf(0);
    expect(query?.app).toBe("sales");
    const minutes =
      (Date.parse(query?.to ?? "") - Date.parse(query?.from ?? "")) / 60_000;
    expect(minutes).toBe(LAST_HOUR.minutes);
    expect(options?.signal).toBeInstanceOf(AbortSignal);
  });

  it("asks for the platform row by its reserved name, and for no app at all to compare them", async () => {
    vi.mocked(getMetrics).mockResolvedValue(contractMetrics());
    const first = renderPanel({ scope: { kind: "platform" } });
    await screen.findByText(/Updated/);
    expect(queryOf(0).query?.app).toBe("_platform");
    first.unmount();

    renderPanel({ scope: { kind: "apps" } });
    await screen.findByRole("table", { name: "Apps compared over the window" });
    expect(queryOf(1).query).not.toHaveProperty("app");
  });

  it("drops the old charts at once when the app changes, and asks for the new app", async () => {
    vi.mocked(getMetrics)
      .mockResolvedValueOnce(contractMetrics())
      .mockReturnValueOnce(new Promise(() => undefined));
    const panel = renderPanel({ scope: { kind: "platform" } });
    await screen.findByText(/Updated/);

    panel.update({ scope: { kind: "app", slug: "sales" } });

    expect(screen.getByText("Loading metrics…")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    await waitFor(() => {
      expect(getMetrics).toHaveBeenCalledTimes(2);
    });
    expect(queryOf(1).query?.app).toBe("sales");
  });

  it("asks again over the new window when the range changes", async () => {
    vi.mocked(getMetrics).mockResolvedValue(contractMetrics());
    const panel = renderPanel({ range: LAST_HOUR });
    await screen.findByText(/Updated/);

    panel.update({ range: LAST_DAY });

    await waitFor(() => {
      expect(getMetrics).toHaveBeenCalledTimes(2);
    });
    const { query } = queryOf(1);
    const minutes =
      (Date.parse(query?.to ?? "") - Date.parse(query?.from ?? "")) / 60_000;
    expect(minutes).toBe(LAST_DAY.minutes);
  });
});

describe("MetricsPanel failures", () => {
  it("says the metrics could not be loaded when the first call fails, and Try again asks once more", async () => {
    vi.mocked(getMetrics)
      .mockRejectedValueOnce(new ApiError(503, "unavailable", "starting up"))
      .mockResolvedValueOnce(contractMetrics());
    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the metrics",
    );
    expect(screen.queryByRole("img")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the last charts under a warning when a refresh fails after a good answer", async () => {
    vi.mocked(getMetrics)
      .mockResolvedValueOnce(contractMetrics())
      .mockRejectedValueOnce(new ApiError(503, "unavailable", "starting up"));
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText(/Updated/);

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not refresh the metrics, showing the last ones received",
    );
    expect(tile("Requests").getByText("412")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Requests per minute" }),
    ).toBeInTheDocument();
  });
});
