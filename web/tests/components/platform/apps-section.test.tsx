import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { AppsSection } from "@/components/platform/apps-section";
import { ApiError, AppList, RequestList, listApps } from "@/lib/api";
import type { PlatformView } from "@/lib/api";
import { carriedOutcome, carryOutcome } from "@/lib/app/settings";
import { PLATFORM_APPS_PATH } from "@/lib/platform/sections";
import type { PollResult } from "@/lib/poll/schedule";
import { readExample } from "../../support/contract-examples";
import { platformView, renderInPlatform } from "./shell-fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  listApps: vi.fn(),
}));

const READY_REQUESTS: PollResult<RequestList> = {
  status: "ready",
  data: RequestList.parse(readExample("RequestList")),
  updatedAtMs: 0,
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listApps).mockResolvedValue(AppList.parse(readExample("AppList")));
});

function setup(
  view: PlatformView = platformView(),
  requests: PollResult<RequestList> = READY_REQUESTS,
) {
  const user = userEvent.setup();
  const page = renderInPlatform(
    <AppsSection view={view} requests={requests} />,
  );
  return { user, view, ...page };
}

/** The slugs of the table's rows, in order, read from each row header's route strip (`/sales/`). */
function listedSlugs(): string[] {
  return screen
    .getAllByRole("rowheader")
    .map((header) => /\/([^/]+)\/$/.exec(header.textContent ?? "")?.[1] ?? "");
}

/** Queries inside the row whose route strip is `/<slug>/`. */
function rowOf(slug: string) {
  const row = screen.getByText(`/${slug}/`).closest("tr");
  if (row === null) throw new Error(`${slug} has no row`);
  return within(row);
}

describe("AppsSection", () => {
  it("lists every app, and the filter narrows by slug, name or owner", async () => {
    const { user } = setup();
    expect(listedSlugs()).toEqual(["sales", "docs", "reports"]);

    const filter = screen.getByRole("searchbox", { name: "Filter apps" });
    await user.type(filter, "weekly");
    expect(listedSlugs()).toEqual(["reports"]);

    await user.clear(filter);
    await user.type(filter, "FRANK@");
    expect(listedSlugs()).toEqual(["docs"]);

    await user.clear(filter);
    await user.type(filter, "sal");
    expect(listedSlugs()).toEqual(["sales"]);
    expect(
      screen.getByRole("table", {
        name: "Registered apps (1 app shown of 3)",
      }),
    ).toBeInTheDocument();
  });

  it("says so when nothing matches, instead of an empty table", async () => {
    const { user } = setup();
    await user.type(
      screen.getByRole("searchbox", { name: "Filter apps" }),
      "zzz",
    );
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText("No app matches the filter")).toBeInTheDocument();
  });

  it("says so when no app is registered", () => {
    setup({ ...platformView(), apps: [] });
    expect(screen.getByText("No apps registered")).toBeInTheDocument();
  });

  it("shows each app's state from the app list, its memory budget and the requests it has waiting", async () => {
    setup();
    expect(await rowOf("docs").findByText("Stopped")).toBeInTheDocument();
    expect(rowOf("sales").getByText("Running")).toBeInTheDocument();
    expect(rowOf("sales").getByText("512 MB")).toBeInTheDocument();
    // sales: one pending settings change; docs: one pending delegation (its denied change does not count).
    expect(rowOf("sales").getByText("1")).toBeInTheDocument();
    expect(rowOf("docs").getByText("1")).toBeInTheDocument();
    expect(rowOf("reports").getByText("0")).toBeInTheDocument();
  });

  it("says a state is unknown when the app list cannot be read, and offers a retry", async () => {
    vi.mocked(listApps).mockRejectedValue(
      new ApiError(503, "unavailable", "starting up"),
    );
    const { user } = setup();
    expect(
      await screen.findByText(/Could not read the apps' states/),
    ).toBeInTheDocument();
    expect(rowOf("sales").getAllByText("Unknown")).toHaveLength(1);
    const calls = vi.mocked(listApps).mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(vi.mocked(listApps).mock.calls.length).toBeGreaterThan(calls);
  });

  it("says the requests waiting are unknown when the request list cannot be read, and retries through the shell", async () => {
    const { user, refresh } = setup(platformView(), {
      status: "failed",
      error: new ApiError(503, "unavailable", "starting up"),
    });
    expect(
      screen.getByText(/Could not read the requests waiting/),
    ).toBeInTheDocument();
    expect(await rowOf("sales").findByText("Running")).toBeInTheDocument();
    expect(rowOf("sales").getByText("Unknown")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("holds the place of a state and a count while their lists load, instead of calling them unknown", () => {
    vi.mocked(listApps).mockReturnValue(new Promise(() => undefined));
    setup(platformView(), { status: "loading" });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading the apps' states and requests",
    );
    expect(screen.queryByText("Unknown")).toBeNull();
  });

  it("leads each row to the app's Settings and Security pages, where it is changed", () => {
    setup();
    // Outside Next's router `next/link` drops the trailing slash that the export keeps.
    expect(
      screen
        .getByRole("link", { name: "Settings of sales" })
        .getAttribute("href"),
    ).toMatch(/^\/app\/settings\/?\?slug=sales$/);
    expect(
      screen
        .getByRole("link", { name: "Security of reports" })
        .getAttribute("href"),
    ).toMatch(/^\/app\/security\/?\?slug=reports$/);
    expect(screen.queryByRole("button", { name: /^Edit/ })).toBeNull();
  });

  it("shows and reads out once what a delete on an app's Settings page did", () => {
    carryOutcome(PLATFORM_APPS_PATH, {
      title: "Deleted reports",
      detail: "Its route is closed.",
    });
    const { announce } = setup();
    expect(screen.getByText("Deleted reports")).toBeInTheDocument();
    expect(announce).toHaveBeenCalledWith("Deleted reports");
    expect(carriedOutcome(PLATFORM_APPS_PATH)).toBeNull();
  });
});
