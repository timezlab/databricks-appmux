import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChartFigure } from "@/components/dashboard/chart-figure";
import { PLOT_MARGIN, Y_AXIS_WIDTH } from "@/components/dashboard/chart-theme";
import { formatAxisNumber } from "@/lib/dashboard/chart";
import { MetricsResponse } from "@/lib/api/schemas";
import { PARTIAL_MARK_OPACITY, columnAxis } from "@/lib/dashboard/chart-data";
import type { ColumnChartSpec } from "@/lib/dashboard/model";
import { CHART_WIDTH_PX, giveChartsASize } from "./chart-sizes";
import {
  DROP_MINUTE,
  ERROR_MINUTE,
  GAP_FIRST_MINUTE,
  GAP_LAST_MINUTE,
  IDLE_MINUTE,
  WINDOW_MINUTES,
  chartSpec,
  isoAt,
  metricsResponse,
  platformHalfHour,
  platformRow,
  seriesModel,
} from "./fixtures";

giveChartsASize();

const MAX_BAR_WIDTH_PX = 32;
/** The card's surface left between two segments of a stack, taken from the upper one. */
const STACK_GAP_PX = 2;

const model = seriesModel(platformHalfHour());

function renderChart(id: Parameters<typeof chartSpec>[1], current = model) {
  const spec = chartSpec(current, id);
  render(<ChartFigure spec={spec} />);
  return { spec, chart: screen.getByRole("img", { name: spec.title }) };
}

describe("a chart on a narrow screen", () => {
  it("scrolls inside a named, focusable region, so the keyboard can reach all of it", () => {
    const { spec, chart } = renderChart("requests");
    const frame = screen.getByRole("region", { name: `${spec.title}, chart` });
    expect(frame).toHaveAttribute("tabindex", "0");
    expect(frame).toContainElement(chart);
  });
});

interface Segment {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** The drawn segments of one series of a stacked chart, by the fill that names it. */
function segments(chart: HTMLElement, fill: string): Segment[] {
  return [...chart.querySelectorAll("rect")]
    .filter((rect) => rect.getAttribute("fill") === fill)
    .map((rect) => ({
      x: Number(rect.getAttribute("x")),
      y: Number(rect.getAttribute("y")),
      width: Number(rect.getAttribute("width")),
      height: Number(rect.getAttribute("height")),
    }));
}

const SUCCEEDED = "var(--chart-1)";
const CLIENT_ERRORS = "url(#texture-requests-warning)";
const SERVER_ERRORS = "url(#texture-requests-danger)";

/** The pointer position over the middle of column `index` of a chart of `count` columns. */
function pointerOver(index: number, count: number) {
  const plotWidth = CHART_WIDTH_PX - Y_AXIS_WIDTH - PLOT_MARGIN.right;
  return {
    clientX: Y_AXIS_WIDTH + ((index + 0.5) * plotWidth) / count,
    clientY: 100,
  };
}

function hover(index: number, count: number): void {
  const wrapper = document.querySelector(".recharts-wrapper");
  if (wrapper === null) throw new Error("the chart is not drawn");
  fireEvent.mouseMove(wrapper, pointerOver(index, count));
}

describe("a chart as a figure", () => {
  it("is one picture with a name and a summary for a screen reader, whatever it draws inside", () => {
    const { spec, chart } = renderChart("requests");
    expect(chart).toHaveAccessibleName("Requests per minute");
    expect(chart).toHaveAccessibleDescription(spec.description);
    expect(spec.description).toMatch(/requests in the window/);
  });

  it("gives every value again as a table, one row per minute, with a minute with no data written out", async () => {
    const user = userEvent.setup();
    renderChart("requests");
    await user.click(screen.getByText("Show the values as a table"));

    const table = screen.getByRole("table", {
      name: "Requests per minute, as a table",
    });
    // The header and a row for every minute of the window, the hole included.
    expect(within(table).getAllByRole("row")).toHaveLength(1 + WINDOW_MINUTES);
    const errorRow = within(table).getByRole("row", {
      name: /08:03 UTC/,
    });
    expect(
      within(errorRow)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["88", "10", "2", "100"]);

    const holeRow = within(table).getByRole("row", { name: /08:11 UTC/ });
    expect(
      within(holeRow)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["no data", "no data", "no data", "no data"]);
  });

  it("names every series of a stack in a legend, in the order they stack, and a single series in none", () => {
    renderChart("requests");
    const legend = screen.getByRole("list", { name: "Legend" });
    expect(
      within(legend)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      "Succeeded (below 400)",
      "Client errors (4xx)",
      "Server errors (5xx)",
    ]);
  });

  it("has no legend for a chart of one series: its title names it", () => {
    renderChart("egress");
    expect(screen.queryByRole("list", { name: "Legend" })).toBeNull();
  });

  it("shows the message instead of an empty chart when there is nothing to draw", () => {
    const quiet = seriesModel(
      metricsResponse([platformRow(0, { requests: 0, r4xx: 0 })]),
    );
    const spec = chartSpec(quiet, "requests");
    render(<ChartFigure spec={spec} />);
    expect(screen.getByText("No requests in this window.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });
});

describe("a chart of stacked columns", () => {
  it("stacks each segment on the one below it, drawn to scale, with a gap between them", () => {
    const { chart } = renderChart("requests");
    const [firstSucceeded] = segments(chart, SUCCEEDED);
    const clientErrors = segments(chart, CLIENT_ERRORS);
    const [firstClientErrors] = clientErrors;
    if (firstSucceeded === undefined || firstClientErrors === undefined) {
      throw new Error("the first minute has both segments");
    }
    // 90 succeeded and 10 failed with a 4xx: the same column, nine to one,
    // the upper segment giving up the gap at its bottom.
    expect(firstClientErrors.x).toBe(firstSucceeded.x);
    expect(
      firstSucceeded.height / (firstClientErrors.height + STACK_GAP_PX),
    ).toBeCloseTo(9, 1);
    expect(
      firstSucceeded.y - (firstClientErrors.y + firstClientErrors.height),
    ).toBeCloseTo(STACK_GAP_PX, 2);
  });

  it("draws a segment only for a value: not for a zero, and not for a minute with no data", () => {
    const { chart } = renderChart("requests");
    const reported = WINDOW_MINUTES - (GAP_LAST_MINUTE - GAP_FIRST_MINUTE + 1);
    // The idle minute was reported with all zeros: it is a row, with no segment.
    expect(segments(chart, SUCCEEDED)).toHaveLength(reported - 1);
    expect(segments(chart, CLIENT_ERRORS)).toHaveLength(reported - 1);
    expect(segments(chart, SERVER_ERRORS)).toHaveLength(1);
    expect(IDLE_MINUTE).toBeGreaterThan(GAP_LAST_MINUTE);
  });

  it("keeps a segment of a few errors among a great many requests at least a pixel high, so it is not lost", () => {
    const busy = seriesModel(
      metricsResponse([
        platformRow(0, { requests: 100_000, r4xx: 0, r5xx: 1 }),
      ]),
    );
    const { chart } = renderChart("requests", busy);
    const [serverErrors] = segments(chart, SERVER_ERRORS);
    expect(serverErrors?.height).toBeGreaterThanOrEqual(1);
  });

  it("never draws a bar wider than a bar may be, however few columns there are", () => {
    const { chart } = renderChart("latency");
    const widths = segments(chart, SUCCEEDED).map((bar) => bar.width);
    expect(widths.length).toBeGreaterThan(0);
    expect(Math.max(...widths)).toBeLessThanOrEqual(MAX_BAR_WIDTH_PX);
  });

  it("shades the minutes with no data between two minutes with data, and only those", () => {
    const { chart } = renderChart("requests");
    const shaded = [...chart.querySelectorAll("path")].filter(
      (path) => path.getAttribute("fill") === "var(--muted)",
    );
    expect(shaded).toHaveLength(1);
    const band = shaded[0];
    const x = Number(band?.getAttribute("x"));
    const width = Number(band?.getAttribute("width"));
    const succeeded = segments(chart, SUCCEEDED);
    // The columns drawn on each side of the hole: minute 9 and minute 15.
    const before = succeeded[GAP_FIRST_MINUTE - 1];
    const after = succeeded[GAP_FIRST_MINUTE];
    if (before === undefined || after === undefined) {
      throw new Error("the hole has a column on each side");
    }
    expect(x).toBeGreaterThanOrEqual(before.x + before.width);
    expect(x + width).toBeLessThanOrEqual(after.x);
    expect(width).toBeGreaterThan((GAP_LAST_MINUTE - GAP_FIRST_MINUTE) * 10);
  });

  it("writes the axis numbers from the scale the data was fitted to, and a time under about every fifth minute", () => {
    const { spec, chart } = renderChart("requests");
    const scale = columnAxis(spec as ColumnChartSpec);
    for (const tick of scale.ticks) {
      expect(
        within(chart).getAllByText(formatAxisNumber(tick, scale.max)).length,
      ).toBeGreaterThan(0);
    }
    for (const time of ["08:00", "08:05", "08:25"]) {
      expect(within(chart).getByText(time)).toBeInTheDocument();
    }
    expect(within(chart).queryByText("08:01")).toBeNull();
  });

  it("labels the columns the model marks, and no others (p50 and p95 of the response time)", () => {
    const { chart } = renderChart("latency");
    expect(within(chart).getByText("p50")).toBeInTheDocument();
    expect(within(chart).getByText("p95")).toBeInTheDocument();
  });

  it("says what a column is when the pointer is over it, even a column with no data", async () => {
    renderChart("requests");
    hover(ERROR_MINUTE, WINDOW_MINUTES);
    expect(
      await screen.findByText(
        "2026-10-30 08:03 UTC: 100 requests, 10 client errors, 2 server errors",
      ),
    ).toBeInTheDocument();

    hover(GAP_FIRST_MINUTE + 1, WINDOW_MINUTES);
    expect(
      await screen.findByText("2026-10-30 08:11 UTC: no data"),
    ).toBeInTheDocument();
  });

  it("draws the dropped packets of the one minute that dropped any", async () => {
    const { chart } = renderChart("egress");
    expect(segments(chart, "url(#texture-egress-warning)")).toHaveLength(1);
    hover(DROP_MINUTE, WINDOW_MINUTES);
    expect(
      await screen.findByText("2026-10-30 08:07 UTC: 4 drops"),
    ).toBeInTheDocument();
  });
});

describe("a line chart", () => {
  it("breaks the line at a minute with no data instead of drawing a zero", () => {
    const { chart } = renderChart("memory");
    const lines = [...chart.querySelectorAll("path")].filter(
      (path) =>
        path.getAttribute("fill") === "none" &&
        path.getAttribute("stroke") === "var(--chart-1)",
    );
    expect(lines).toHaveLength(1);
    // One piece before the hole, one after it.
    expect(lines[0]?.getAttribute("d")?.match(/M/g)).toHaveLength(2);
  });

  it("draws the limit as a dashed line of its own, with a different marker and a label on the chart", () => {
    const { chart } = renderChart("memory");
    const limit = [...chart.querySelectorAll("path")].find(
      (path) => path.getAttribute("stroke") === "var(--chart-2)",
    );
    expect(limit?.getAttribute("stroke-dasharray")).toBeTruthy();
    expect(within(chart).getByText("Memory limit")).toBeInTheDocument();
    // The newest reading is a circle, the end of the limit a diamond.
    expect(chart.querySelectorAll("circle")).toHaveLength(1);
    expect(chart.querySelectorAll("polygon")).toHaveLength(1);
  });

  it("names the used memory and the limit in a legend with their dash and marker, not only their colour", () => {
    renderChart("memory");
    const legend = screen.getByRole("list", { name: "Legend" });
    const [used, limit] = within(legend).getAllByRole("listitem");
    expect(used).toHaveTextContent("Memory used");
    expect(limit).toHaveTextContent("Memory limit");
    expect(used?.querySelector("circle")).not.toBeNull();
    expect(limit?.querySelector("polygon")).not.toBeNull();
    expect(
      limit?.querySelector("line")?.getAttribute("stroke-dasharray"),
    ).toBeTruthy();
    expect(
      used?.querySelector("line")?.getAttribute("stroke-dasharray"),
    ).toBeNull();
  });

  it("has no limit line when the platform reports no limit", () => {
    const unlimited = seriesModel(
      metricsResponse([
        platformRow(0, { mem_limit_mb: null }),
        platformRow(1, { mem_limit_mb: null }),
      ]),
    );
    const { chart } = renderChart("memory", unlimited);
    expect(within(chart).queryByText("Memory limit")).toBeNull();
    expect(chart.querySelectorAll("polygon")).toHaveLength(0);
    expect(screen.queryByRole("list", { name: "Legend" })).toBeNull();
  });

  it("says what a minute is when the pointer is over it", async () => {
    renderChart("memory");
    hover(ERROR_MINUTE, WINDOW_MINUTES - 1);
    expect(
      await screen.findByText(/^2026-10-30 08:0[2-4] UTC: 5,0[2-4]0 MB$/),
    ).toBeInTheDocument();
  });
});

describe("a bucket the window cuts short", () => {
  // 08:01 to 09:14 is 73 minutes: 2-minute buckets from 08:00, so the first
  // column holds one minute of the window and the rest hold two.
  const cut = seriesModel(
    MetricsResponse.parse({
      from: isoAt(1),
      to: isoAt(74),
      truncated: false,
      items: Array.from({ length: 73 }, (_, offset) => platformRow(offset + 1)),
    }),
  );
  const faint = String(PARTIAL_MARK_OPACITY);

  /** The drawn segments of the chart (not the tiles of its texture patterns), leftmost first. */
  function drawnSegments(chart: HTMLElement): Element[] {
    return [...chart.querySelectorAll("rect")]
      .filter(
        (rect) =>
          rect.closest("defs") === null &&
          [SUCCEEDED, CLIENT_ERRORS, SERVER_ERRORS].includes(
            rect.getAttribute("fill") ?? "",
          ),
      )
      .toSorted(
        (left, right) =>
          Number(left.getAttribute("x")) - Number(right.getAttribute("x")),
      );
  }

  it("draws every segment of its column fainter, and the other columns at full strength", () => {
    const { chart } = renderChart("requests", cut);
    const drawn = drawnSegments(chart);
    const firstX = drawn[0]?.getAttribute("x");
    const [inFirst, inOthers] = [
      drawn.filter((rect) => rect.getAttribute("x") === firstX),
      drawn.filter((rect) => rect.getAttribute("x") !== firstX),
    ];
    // Its succeeded requests and its client errors.
    expect(inFirst).toHaveLength(2);
    for (const rect of inFirst) {
      expect(rect).toHaveAttribute("fill-opacity", faint);
    }
    expect(inOthers.length).toBeGreaterThan(0);
    for (const rect of inOthers) {
      expect(rect).not.toHaveAttribute("fill-opacity");
    }
  });

  it("says under the axis what a fainter column is, and the tooltip still says (partial)", () => {
    const { spec } = renderChart("requests", cut);
    expect(
      screen.getByText(/A fainter column is a partial bucket/),
    ).toBeInTheDocument();
    expect(spec.tips[0]).toMatch(/\(partial\)/);
  });

  it("gives a line a fainter marker at that point, and keeps the newest point's marker whole", () => {
    const { chart } = renderChart("memory", cut);
    const markers = [...chart.querySelectorAll("circle")];
    expect(
      markers.map((marker) => marker.getAttribute("fill-opacity")),
    ).toEqual([faint, null]);
    expect(
      screen.getByText(/A fainter point is a partial bucket/),
    ).toBeInTheDocument();
  });

  it("is not mentioned under a chart whose buckets are all whole", () => {
    renderChart("requests");
    expect(screen.queryByText(/partial bucket/)).toBeNull();
  });
});
