import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { DashboardView } from "@/components/dashboard/dashboard-view";
import { ApiError, getMe, getMetrics, listApps } from "@/lib/api";
import type { AppList, MeResponse } from "@/lib/api";
import { TIME_RANGES } from "@/lib/dashboard/scope";
import { readExample } from "../../support/contract-examples";
import { giveChartsASize } from "./chart-sizes";
import { contractMetrics } from "./fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  getMe: vi.fn(),
  listApps: vi.fn(),
  getMetrics: vi.fn(),
}));

giveChartsASize();

const PLATFORM_LABEL = "Platform: all traffic and memory";
const COMPARISON_LABEL = "All apps: comparison table";
const SALES_LABEL = "Sales dashboard (sales)";
const [, , , LAST_DAY] = TIME_RANGES;

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
});

function windowMinutes(call: number): number {
  const [query] = vi.mocked(getMetrics).mock.calls[call] ?? [];
  return (Date.parse(query?.to ?? "") - Date.parse(query?.from ?? "")) / 60_000;
}

describe("DashboardView", () => {
  it("reads who you are and your apps with an abort signal, then charts the platform for a platform admin", async () => {
    render(<DashboardView />);

    expect(
      screen.getByRole("heading", { name: "Dashboard", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("Loading the dashboard…")).toBeInTheDocument();
    expect(await screen.findByText(/Updated/)).toBeInTheDocument();

    for (const load of [getMe, listApps]) {
      const [options] = vi.mocked(load).mock.calls[0] ?? [];
      expect(options?.signal).toBeInstanceOf(AbortSignal);
    }
    expect(screen.getByLabelText("Show")).toHaveDisplayValue(PLATFORM_LABEL);
    expect(screen.getByLabelText("Time range")).toHaveDisplayValue("Last hour");
    expect(vi.mocked(getMetrics).mock.calls[0]?.[0]?.app).toBe("_platform");
  });

  it("offers a platform admin the platform, the comparison and every app; an app admin only their apps", async () => {
    const admin = render(<DashboardView />);
    const show = await screen.findByLabelText("Show");
    expect(
      within(show)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      PLATFORM_LABEL,
      COMPARISON_LABEL,
      "Team docs (docs)",
      "Weekly reports (reports)",
      SALES_LABEL,
    ]);
    admin.unmount();

    vi.mocked(getMe).mockResolvedValue(me(false));
    render(<DashboardView />);
    const appShow = await screen.findByLabelText("Show");
    const options = within(appShow)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).not.toContain(PLATFORM_LABEL);
    expect(options).not.toContain(COMPARISON_LABEL);
    expect(options).toHaveLength(3);
    // The first app, by slug, is charted without a choice.
    expect(appShow).toHaveDisplayValue("Team docs (docs)");
    await waitFor(() => {
      expect(vi.mocked(getMetrics).mock.calls.at(-1)?.[0]?.app).toBe("docs");
    });
  });

  it("charts the app chosen in Show, dropping the old charts and asking for that app", async () => {
    const user = userEvent.setup();
    render(<DashboardView />);
    await screen.findByText(/Updated/);

    await user.selectOptions(screen.getByLabelText("Show"), SALES_LABEL);

    await waitFor(() => {
      expect(vi.mocked(getMetrics).mock.calls.at(-1)?.[0]?.app).toBe("sales");
    });
    expect(screen.getByLabelText("Show")).toHaveDisplayValue(SALES_LABEL);
    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
  });

  it("asks again over a longer window when the time range changes", async () => {
    const user = userEvent.setup();
    render(<DashboardView />);
    await screen.findByText(/Updated/);
    expect(windowMinutes(0)).toBe(60);

    await user.selectOptions(
      screen.getByLabelText("Time range"),
      LAST_DAY.label,
    );

    await waitFor(() => {
      expect(getMetrics).toHaveBeenCalledTimes(2);
    });
    expect(windowMinutes(1)).toBe(LAST_DAY.minutes);
    expect(screen.getByLabelText("Time range")).toHaveDisplayValue(
      LAST_DAY.label,
    );
  });

  it("jumps from the comparison table to one app's charts", async () => {
    const user = userEvent.setup();
    render(<DashboardView />);
    await screen.findByText(/Updated/);
    await user.selectOptions(screen.getByLabelText("Show"), COMPARISON_LABEL);

    await user.click(
      await screen.findByRole("button", { name: /^View charts\s*for sales$/ }),
    );

    expect(screen.getByLabelText("Show")).toHaveDisplayValue(SALES_LABEL);
    await waitFor(() => {
      expect(vi.mocked(getMetrics).mock.calls.at(-1)?.[0]?.app).toBe("sales");
    });
  });

  it("shows nothing to chart, and asks for no metrics, to someone who manages no app", async () => {
    vi.mocked(getMe).mockResolvedValue(me(false));
    vi.mocked(listApps).mockResolvedValue(appsWithoutManage());
    render(<DashboardView />);

    expect(await screen.findByText("No dashboard to show")).toBeInTheDocument();
    expect(screen.queryByLabelText("Show")).toBeNull();
    expect(getMetrics).not.toHaveBeenCalled();
  });

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
    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
  });
});
