import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { BreakdownPanel } from "@/components/dashboard/breakdown-panel";
import { ApiError, getMetricsBreakdown } from "@/lib/api";
import { appPagePath } from "@/lib/app/params";
import { MetricsBreakdown } from "@/lib/api/schemas";
import { TIME_RANGES } from "@/lib/dashboard/scope";
import { readExample } from "../../support/contract-examples";
import { giveChartsASize } from "./chart-sizes";
import { fakeMatchMedia } from "./match-media";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  getMetricsBreakdown: vi.fn(),
}));

giveChartsASize();
fakeMatchMedia();

const [, LAST_HOUR, , , LAST_3_DAYS] = TIME_RANGES;
const MINUTE_MS = 60_000;

/** The contract example: "docs" (Docs Portal), "old-reports" and the rest as `other`, at 15 minutes. */
function example(): MetricsBreakdown {
  return MetricsBreakdown.parse(readExample("MetricsBreakdown"));
}

/**
 * Where a link goes, without the trailing slash of its path: `next/link` drops
 * it under the tests, which do not load the export's `trailingSlash`.
 */
function linkTarget(link: HTMLElement | string | null): string {
  const href = typeof link === "string" ? link : link?.getAttribute("href");
  const url = new URL(href ?? "", "http://admin.test");
  return `${url.pathname.replace(/\/$/, "")}${url.search}`;
}

beforeEach(() => {
  vi.mocked(getMetricsBreakdown).mockReset();
});

describe("BreakdownPanel", () => {
  it("asks for the range at the step the platform's charts use, for the eight busiest apps", async () => {
    vi.mocked(getMetricsBreakdown).mockResolvedValue(example());
    render(<BreakdownPanel range={LAST_3_DAYS} />);
    await screen.findByText(/Updated/);

    const [query, options] = vi.mocked(getMetricsBreakdown).mock.calls[0] ?? [];
    expect(query).toMatchObject({ step: "5m", limit: 8 });
    expect(
      (Date.parse(query?.to ?? "") - Date.parse(query?.from ?? "")) / MINUTE_MS,
    ).toBe(LAST_3_DAYS.minutes);
    expect(options?.signal).toBeInstanceOf(AbortSignal);
  });

  it("stacks the apps in three framed charts whose legend names each app by its route strip", async () => {
    vi.mocked(getMetricsBreakdown).mockResolvedValue(example());
    render(<BreakdownPanel range={LAST_HOUR} />);
    await screen.findByText(/Updated/);

    for (const title of [
      "Requests by app, per 15 minutes",
      "Memory peak by app, per 15 minutes",
      "Server errors by app, per 15 minutes",
    ]) {
      const frame = screen.getByRole("group", { name: title });
      expect(
        within(frame).getByRole("img", { name: title }),
      ).toBeInTheDocument();
    }
    const legend = within(
      screen.getByRole("group", { name: "Requests by app, per 15 minutes" }),
    ).getByRole("list", { name: "Legend" });
    expect(
      within(legend)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Docs Portal/docs/", "/old-reports/", "Other apps"]);
  });

  it("lists the apps busiest first, each linked to its Insights, an app no longer registered and the rest last unlinked", async () => {
    vi.mocked(getMetricsBreakdown).mockResolvedValue(example());
    render(<BreakdownPanel range={LAST_HOUR} />);
    await screen.findByText(/Updated/);

    const table = screen.getByRole("table", {
      name: "Apps over the window, busiest first",
    });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent?.split("/")[0])).toEqual([
      expect.stringContaining("Docs Portal"),
      expect.stringContaining("old-reports"),
      expect.stringContaining("Other apps"),
    ]);
    expect(
      linkTarget(within(table).getByRole("link", { name: "Docs Portal" })),
    ).toBe(linkTarget(appPagePath("docs", "insights")));
    // The server sends display_name null for an app no longer registered: its
    // Insights page would only say the app is not found.
    expect(
      within(table).queryByRole("link", { name: /old-reports/ }),
    ).toBeNull();
    expect(rows[1]).toHaveTextContent(/old-reports.*no longer registered/);
    expect(
      within(table).queryByRole("link", { name: "Other apps" }),
    ).toBeNull();
  });

  it("says no app wrote metrics when the answer holds none", async () => {
    vi.mocked(getMetricsBreakdown).mockResolvedValue({
      ...example(),
      series: [],
      other: null,
    });
    render(<BreakdownPanel range={LAST_HOUR} />);

    expect(
      await screen.findByText("No app traffic in the last hour"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("keeps each chart's place, and offers to try again, when the breakdown cannot be read", async () => {
    vi.mocked(getMetricsBreakdown).mockRejectedValue(
      new ApiError(503, "unavailable", "starting up"),
    );
    render(<BreakdownPanel range={LAST_HOUR} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the metrics by app",
    );
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "Requests by app" }),
    ).toHaveTextContent("Not shown: the metrics could not be read.");
  });
});
