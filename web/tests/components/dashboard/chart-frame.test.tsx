import { act, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChartFrame } from "@/components/dashboard/chart-frame";
import { giveChartsASize } from "./chart-sizes";
import {
  chartSpec,
  metricsResponse,
  platformHalfHour,
  platformRow,
  seriesModel,
} from "./fixtures";
import { fakeMatchMedia } from "./match-media";

giveChartsASize();
const media = fakeMatchMedia();

const model = seriesModel(platformHalfHour());

function frame(name: string) {
  return screen.getByRole("group", { name });
}

describe("a chart frame", () => {
  it("keeps the chart's place while it loads: its name, and a skeleton hidden from screen readers", () => {
    render(<ChartFrame status="loading" name="Requests" />);

    const loading = frame("Requests");
    expect(loading).toHaveAttribute("aria-busy", "true");
    expect(
      within(loading).getByRole("heading", { level: 3, name: "Requests" }),
    ).toBeInTheDocument();
    const skeleton = loading.querySelector('[data-skeleton="chart"]');
    expect(skeleton).toHaveAttribute("aria-hidden", "true");
    expect(within(loading).queryByRole("img")).toBeNull();
  });

  it("says, without a picture, that its chart could not be read", () => {
    render(<ChartFrame status="failed" name="Requests" />);

    const failed = frame("Requests");
    expect(failed).not.toHaveAttribute("aria-busy");
    expect(
      within(failed).getByText("Not shown: the metrics could not be read."),
    ).toBeInTheDocument();
    expect(failed.querySelector("[data-skeleton]")).toBeNull();
    expect(within(failed).queryByRole("img")).toBeNull();
  });

  it("draws the chart under its title when it is ready, with what the frame adds below it", () => {
    const spec = chartSpec(model, "requests");
    render(
      <ChartFrame status="ready" spec={spec}>
        <p>Added below</p>
      </ChartFrame>,
    );

    const ready = frame("Requests per minute");
    expect(
      within(ready).getByRole("img", { name: "Requests per minute" }),
    ).toBeInTheDocument();
    expect(within(ready).getByText("Added below")).toBeInTheDocument();
    expect(ready.querySelector("[data-skeleton]")).toBeNull();
  });

  it("says there was nothing to draw when the chart is ready but empty", () => {
    const quiet = seriesModel(
      metricsResponse([platformRow(0, { requests: 0, r4xx: 0 })]),
    );
    render(<ChartFrame status="ready" spec={chartSpec(quiet, "requests")} />);

    const empty = frame("Requests per minute");
    expect(
      within(empty).getByText("No requests in this window."),
    ).toBeInTheDocument();
    expect(within(empty).queryByRole("img")).toBeNull();
  });
});

describe("a chart frame when the colour scheme changes", () => {
  it("draws its chart again, so the texture patterns take the new colours", () => {
    render(<ChartFrame status="ready" spec={chartSpec(model, "requests")} />);
    const before = screen.getByRole("img", { name: "Requests per minute" });

    act(() => {
      media.setPrefersDark(true);
    });

    const after = screen.getByRole("img", { name: "Requests per minute" });
    expect(after).not.toBe(before);
    expect(before).not.toBeInTheDocument();
    act(() => {
      media.setPrefersDark(false);
    });
  });
});
