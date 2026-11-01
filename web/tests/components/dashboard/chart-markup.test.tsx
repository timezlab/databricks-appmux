import { act } from "@testing-library/react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartFigure } from "@/components/dashboard/chart-figure";
import { giveChartsASize } from "./chart-sizes";
import { chartSpec, platformHalfHour, seriesModel } from "./fixtures";

// The edge serves the admin UI under `style-src 'self'`: the prerendered HTML
// may not hold a style attribute. Recharts measures its container and writes
// styles, so a chart is drawn only once the browser has taken over, and the
// server HTML holds a placeholder of the chart's height in its place.

giveChartsASize();

afterEach(() => {
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

const model = seriesModel(platformHalfHour());

describe("the prerendered chart", () => {
  it.each(model.charts.map((spec) => [spec.id, spec] as const))(
    "%s has no style attribute and no drawn chart, but its name, summary and table",
    (_id, spec) => {
      const html = renderToString(<ChartFigure spec={spec} />);
      expect(html).not.toMatch(/\sstyle\s*=/i);
      expect(html).not.toMatch(/<style[\s>]/i);
      expect(html).not.toContain("recharts");
      expect(html).toContain(`aria-label="${spec.title}"`);
      expect(html).toContain("<table");
      expect(html).toContain('data-slot="skeleton"');
    },
  );

  it("is taken over without a hydration mismatch, and then draws the chart", async () => {
    const spec = chartSpec(model, "requests");
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ChartFigure spec={spec} />);
    document.body.append(container);
    expect(container.querySelector("svg.recharts-surface")).toBeNull();

    const errors = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    await act(async () => {
      hydrateRoot(container, <ChartFigure spec={spec} />);
      // Let React's work after the first paint (the switch from placeholder to chart) finish.
      await Promise.resolve();
    });

    expect(errors).not.toHaveBeenCalled();
    expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    expect(container.querySelector('[data-slot="skeleton"]')).toBeNull();
  });
});
