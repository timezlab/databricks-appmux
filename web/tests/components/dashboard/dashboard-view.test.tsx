import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { DashboardView } from "@/components/dashboard/dashboard-view";
import {
  ApiError,
  getMe,
  getMetrics,
  getMetricsBreakdown,
  listApps,
} from "@/lib/api";
import type { AppList, MeResponse } from "@/lib/api";
import { MetricsBreakdown } from "@/lib/api/schemas";
import { TIME_RANGES } from "@/lib/dashboard/scope";
import { readExample } from "../../support/contract-examples";
import { giveChartsASize } from "./chart-sizes";
import { contractMetrics } from "./fixtures";
import { fakeMatchMedia } from "./match-media";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  getMe: vi.fn(),
  listApps: vi.fn(),
  getMetrics: vi.fn(),
  getMetricsBreakdown: vi.fn(),
}));

giveChartsASize();
fakeMatchMedia();

const LAST_WEEK = TIME_RANGES[5];
const MINUTE_MS = 60_000;

// The contract's example: three apps, all CAN_MANAGE.
const APPS = readExample("AppList") as AppList;

function me(isPlatformAdmin: boolean): MeResponse {
  return {
    email: "dana@example.com",
    groups: ["data-platform"],
    is_platform_admin: isPlatformAdmin,
  };
}

function appsWithoutManage(): AppList {
  return {
    items: APPS.items.map((app) => ({ ...app, permission: "CAN_USE" })),
  };
}

beforeEach(() => {
  vi.mocked(getMe).mockReset().mockResolvedValue(me(true));
  vi.mocked(listApps).mockReset().mockResolvedValue(APPS);
  vi.mocked(getMetrics).mockReset().mockResolvedValue(contractMetrics());
  vi.mocked(getMetricsBreakdown)
    .mockReset()
    .mockResolvedValue(MetricsBreakdown.parse(readExample("MetricsBreakdown")));
});

function windowMinutes(query: { from?: string; to?: string } | undefined) {
  return (
    (Date.parse(query?.to ?? "") - Date.parse(query?.from ?? "")) / MINUTE_MS
  );
}

function rangeToggle(shortLabel: string) {
  return screen.getByRole("radio", { name: new RegExp(`^${shortLabel},`) });
}

describe("DashboardView for a platform admin", () => {
  it("reads who you are and your apps with an abort signal, then charts the platform and its breakdown by app", async () => {
    render(<DashboardView />);

    expect(
      screen.getByRole("heading", { name: "Dashboard", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("Loading the dashboard…")).toBeInTheDocument();
    expect(
      await screen.findByRole("group", { name: "Requests per minute" }),
    ).toBeInTheDocument();

    for (const load of [getMe, listApps]) {
      const [options] = vi.mocked(load).mock.calls[0] ?? [];
      expect(options?.signal).toBeInstanceOf(AbortSignal);
    }
    expect(
      screen.getByRole("heading", { level: 2, name: "Platform" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "By app" }),
    ).toBeInTheDocument();
    expect(vi.mocked(getMetrics).mock.calls[0]?.[0]?.app).toBe("_platform");
    expect(
      await screen.findByRole("group", {
        name: "Requests by app, per 15 minutes",
      }),
    ).toBeInTheDocument();
    expect(rangeToggle("1h")).toHaveAttribute("aria-checked", "true");
  });

  it("reads the platform and the breakdown over 7 days at 15 minutes when 7d is pressed", async () => {
    const user = userEvent.setup();
    render(<DashboardView />);
    await screen.findByRole("group", { name: "Requests per minute" });
    vi.mocked(getMetrics).mockClear();
    vi.mocked(getMetricsBreakdown).mockClear();

    await user.click(rangeToggle("7d"));

    await waitFor(() => {
      expect(getMetricsBreakdown).toHaveBeenCalledTimes(1);
    });
    expect(rangeToggle("7d")).toHaveAttribute("aria-checked", "true");
    // Only the window of the range: the week before it is no longer kept.
    expect(getMetrics).toHaveBeenCalledTimes(1);
    const [metricsQuery] = vi.mocked(getMetrics).mock.calls[0] ?? [];
    const [breakdownQuery] = vi.mocked(getMetricsBreakdown).mock.calls[0] ?? [];
    expect(metricsQuery).toMatchObject({ app: "_platform", step: "15m" });
    expect(breakdownQuery).toMatchObject({ step: "15m", limit: 8 });
    expect(windowMinutes(metricsQuery)).toBe(LAST_WEEK.minutes);
    expect(windowMinutes(breakdownQuery)).toBe(LAST_WEEK.minutes);
  });

  it("keeps a range chosen when the pressed toggle is pressed again", async () => {
    const user = userEvent.setup();
    render(<DashboardView />);
    await screen.findByRole("group", { name: "Requests per minute" });

    await user.click(rangeToggle("1h"));

    expect(rangeToggle("1h")).toHaveAttribute("aria-checked", "true");
  });
});

describe("DashboardView for someone else", () => {
  it("lists the apps they manage, each linked to its Insights, and asks for no metrics", async () => {
    vi.mocked(getMe).mockResolvedValue(me(false));
    render(<DashboardView />);

    const section = await screen.findByRole("region", {
      name: "Insights of your apps",
    });
    const links = within(section).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual([
      "Team docs: Insights",
      "Weekly reports: Insights",
      "Sales dashboard: Insights",
    ]);
    expect(getMetrics).not.toHaveBeenCalled();
    expect(getMetricsBreakdown).not.toHaveBeenCalled();
  });

  it("shows nothing to chart to someone who manages no app", async () => {
    vi.mocked(getMe).mockResolvedValue(me(false));
    vi.mocked(listApps).mockResolvedValue(appsWithoutManage());
    render(<DashboardView />);

    expect(await screen.findByText("No dashboard to show")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(getMetrics).not.toHaveBeenCalled();
  });
});

describe("DashboardView failures", () => {
  it("says the dashboard could not load when who you are cannot be read, and Try again asks once more", async () => {
    vi.mocked(getMe)
      .mockRejectedValueOnce(new ApiError(503, "unavailable", "starting up"))
      .mockResolvedValue(me(true));
    const user = userEvent.setup();
    render(<DashboardView />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load who you are and your apps",
    );
    expect(getMetrics).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("group", { name: "Requests per minute" }),
    ).toBeInTheDocument();
  });
});
