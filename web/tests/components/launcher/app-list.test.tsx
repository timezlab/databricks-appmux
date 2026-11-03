import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppsSection } from "@/components/launcher/app-list";
import { API_BASE_URL, ApiError, AppList } from "@/lib/api";
import { APP_FILTER_MIN_COUNT } from "@/lib/launcher/apps";
import { appsEmptyCopy } from "@/lib/launcher/page";
import type { PollResult } from "@/lib/poll/schedule";
import { appLink } from "@/lib/slug-link";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

afterEach(() => {
  vi.restoreAllMocks();
});

// The contract's example: "sales" is running and offers both open modes,
// "docs" is stopped, "reports" is running with nothing to choose. All three
// are CAN_MANAGE.
const EXAMPLE_APPS = AppList.parse(readExample("AppList"));

function ready(data: AppList): PollResult<AppList> {
  return { status: "ready", data, updatedAtMs: 0 };
}

function renderApps(
  apps: PollResult<AppList>,
  { isPlatformAdmin = false, onReload = vi.fn() } = {},
) {
  render(
    <AppsSection
      apps={apps}
      isPlatformAdmin={isPlatformAdmin}
      onReload={onReload}
    />,
  );
  return { onReload, user: userEvent.setup() };
}

function rowOf(displayName: string): HTMLElement {
  const header = screen.getByRole("rowheader", {
    name: new RegExp(`^${displayName}`),
  });
  const row = header.closest("tr");
  if (row === null) throw new Error(`No row for ${displayName}`);
  return row;
}

function visitorApps(): AppList {
  return {
    items: [
      {
        slug: "wiki",
        display_name: "Team wiki",
        state: "running" as const,
        permission: "CAN_USE" as const,
        open_modes: [],
      },
      {
        slug: "docs",
        display_name: "Team docs",
        state: "stopped" as const,
        permission: "CAN_USE" as const,
        open_modes: [],
      },
    ],
  };
}

// The visible label and the app's name (a `sr-only` span) make one accessible
// name; dom-accessibility-api drops the space between them that a browser keeps.
function labelledBy(label: string, appName: string): RegExp {
  return new RegExp(`^${label}\\s*${appName}$`);
}

function sentJson(call: RecordedCall | undefined): unknown {
  const body = call?.init.body;
  if (typeof body !== "string") throw new Error("the call has no JSON body");
  return JSON.parse(body);
}

function manyApps(count: number): AppList {
  return {
    items: Array.from({ length: count }, (_unused, index) => ({
      slug: `app-${index + 1}`,
      display_name: `App number ${index + 1}`,
      state: "running" as const,
      permission: "CAN_USE" as const,
      open_modes: [],
    })),
  };
}

describe("AppsSection", () => {
  it("draws one table row per app with its name, slug and state", () => {
    renderApps(ready(EXAMPLE_APPS));

    const table = screen.getByRole("table", { name: "Apps you can open" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual(["Name", "State", "Actions"]);
    // The header row and one row per app.
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    const docs = within(rowOf("Team docs"));
    // The slug is shown as the app's route, on the route strip.
    expect(docs.getByText("/docs/")).toBeInTheDocument();
    expect(docs.getByText(/Stopped/)).toBeInTheDocument();
    expect(
      within(rowOf("Weekly reports")).getByText(/Running/),
    ).toBeInTheDocument();
  });

  it("lists the apps by name, whatever order the server sent them in", () => {
    renderApps(ready(EXAMPLE_APPS));

    const names = screen
      .getAllByRole("rowheader")
      .map((header) => header.textContent);
    expect(names).toEqual([
      expect.stringMatching(/^Sales dashboard/),
      expect.stringMatching(/^Team docs/),
      expect.stringMatching(/^Weekly reports/),
    ]);
  });

  it("links a running app to the address appLink() builds, and a stopped one nowhere, saying why", () => {
    renderApps(ready(EXAMPLE_APPS));

    const open = screen.getByRole("link", {
      name: labelledBy("Open", "Weekly reports"),
    });
    expect(open).toHaveAttribute("href", appLink("reports"));

    const docs = within(rowOf("Team docs"));
    expect(docs.queryByRole("link", { name: /^Open/ })).toBeNull();
    expect(docs.getByText(/Not running\./)).toBeInTheDocument();
  });

  it("tells a visitor who cannot manage a stopped app to ask its admin, and offers no Manage", () => {
    renderApps(ready(visitorApps()));

    const docs = within(rowOf("Team docs"));
    expect(
      docs.getByText("Not running. Ask the app admin to start it."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Manage/ })).toBeNull();
  });

  it("sends a manager of a stopped app to Manage, where it is started", () => {
    renderApps(ready(EXAMPLE_APPS));

    expect(
      within(rowOf("Team docs")).getByText(
        "Not running. Start it from Manage.",
      ),
    ).toBeInTheDocument();
  });

  it("links Manage to the app page of the app, named by the app, for an app the visitor manages", () => {
    renderApps(ready(EXAMPLE_APPS));

    const manage = screen.getByRole("link", {
      name: labelledBy("Manage", "Sales dashboard"),
    });
    // next/link adds the base path and the trailing slash as the build is
    // configured; what must be right here is the app the link names.
    const href = new URL(
      manage.getAttribute("href") ?? "",
      "http://appmux.test",
    );
    expect(href.searchParams.get("slug")).toBe("sales");
  });

  it("offers both ways to open an app that needs a choice, the main one last, and explains them in a closed disclosure", () => {
    renderApps(ready(EXAMPLE_APPS));

    const sales = within(rowOf("Sales dashboard"));
    const buttons = sales.getAllByRole("button");
    expect(buttons).toHaveLength(2);
    expect(buttons[0]).toHaveTextContent(/^Open as a user$/);
    expect(buttons[1]).toHaveTextContent(/^Open without token/);
    expect(sales.queryByRole("link", { name: /^Open/ })).toBeNull();

    const summary = screen.getByText("What is the difference?");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText(/Your choice is remembered/)).toBeInTheDocument();
  });

  it("says nothing about open modes to a visitor who is offered one way to open", () => {
    renderApps(ready(visitorApps()));

    expect(screen.queryByText("What is the difference?")).toBeNull();
    expect(screen.queryByText(/Open as a user/)).toBeNull();
  });

  it("asks before opening as a user and calls the API only for the mode that was chosen", async () => {
    const calls: RecordedCall[] = [];
    stubFetch((call) => {
      calls.push(call);
      return jsonResponse(503, {
        error: { code: "unavailable", message: "try again later" },
      });
    });
    const { user } = renderApps(ready(EXAMPLE_APPS));
    const sales = within(rowOf("Sales dashboard"));

    await user.click(sales.getByRole("button", { name: "Open as a user" }));
    expect(
      screen.getByRole("group", { name: /Open Sales dashboard as a user\?/ }),
    ).toBeInTheDocument();
    expect(calls).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(calls).toHaveLength(0);

    await user.click(
      sales.getByRole("button", {
        name: labelledBy("Open without token", "Sales dashboard"),
      }),
    );
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(`${API_BASE_URL}/apps/sales/open-mode`);
    expect(calls[0]?.init.method).toBe("POST");
    expect(sentJson(calls[0])).toEqual({
      mode: "without_token",
    });
  });

  it("says that the app could not be opened when the mode cannot be saved", async () => {
    stubFetch(() =>
      jsonResponse(503, {
        error: { code: "unavailable", message: "try again later" },
      }),
    );
    const { user } = renderApps(ready(EXAMPLE_APPS));

    await user.click(
      screen.getByRole("button", {
        name: labelledBy("Open without token", "Sales dashboard"),
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not open Sales dashboard",
    );
  });

  it.each([
    [true, appsEmptyCopy(true)],
    [false, appsEmptyCopy(false)],
  ])(
    "says there is nothing to open when the list is empty (platform admin: %s)",
    (isPlatformAdmin, copy) => {
      renderApps(ready({ items: [] }), { isPlatformAdmin });

      expect(screen.getByText(copy.title)).toBeInTheDocument();
      expect(screen.getByText(copy.body)).toBeInTheDocument();
      expect(screen.queryByRole("table")).toBeNull();
    },
  );

  it("offers a search only from APP_FILTER_MIN_COUNT apps, narrows the rows, and clears back", async () => {
    const { user } = renderApps(ready(manyApps(APP_FILTER_MIN_COUNT)));

    await user.type(
      screen.getByRole("searchbox", { name: /Search apps/ }),
      "7",
    );
    expect(screen.getAllByRole("rowheader")).toHaveLength(1);
    expect(screen.getByRole("rowheader")).toHaveTextContent(/^App number 7/);
    expect(
      screen.getByText(`1 of ${APP_FILTER_MIN_COUNT} apps match.`),
    ).toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox", { name: /Search apps/ }));
    await user.type(
      screen.getByRole("searchbox", { name: /Search apps/ }),
      "no such app",
    );
    expect(screen.getByText("No app matches the search")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Clear the search" }));
    expect(screen.getAllByRole("rowheader")).toHaveLength(APP_FILTER_MIN_COUNT);
    expect(screen.getByRole("searchbox", { name: /Search apps/ })).toHaveValue(
      "",
    );
  });

  it("has no search below APP_FILTER_MIN_COUNT apps", () => {
    renderApps(ready(manyApps(APP_FILTER_MIN_COUNT - 1)));

    expect(screen.queryByRole("searchbox")).toBeNull();
  });

  it("shows the failure of the first load and reloads on request", async () => {
    const { user, onReload } = renderApps({
      status: "failed",
      error: new ApiError(409, "conflict", "The list changed."),
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not load your apps",
    );
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });
});
