import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { AppsSection } from "@/components/platform/apps-section";
import { updateApp } from "@/lib/api";
import type { PlatformView } from "@/lib/api";
import { appDetail, platformView, renderInPlatform } from "./shell-fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  updateApp: vi.fn(),
  setOwner: vi.fn(),
  putAppEgress: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
});

function setup(view: PlatformView = platformView()) {
  const user = userEvent.setup();
  const page = renderInPlatform(<AppsSection view={view} />);
  return { user, view, ...page };
}

/** The slugs in the table's row headers, in order. */
function listedSlugs(): string[] {
  return screen
    .getAllByRole("rowheader")
    .map((header) => header.textContent ?? "");
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
    const view = platformView();
    setup({ ...view, apps: [] });
    expect(screen.getByText("No apps registered")).toBeInTheDocument();
  });

  it("shows each app's delegations and memory budget as words and numbers", () => {
    setup();
    const salesRow = screen
      .getByRole("rowheader", { name: "sales" })
      .closest("tr");
    if (salesRow === null) throw new Error("sales has no row");
    const cells = within(salesRow);
    expect(cells.getByText("OBO")).toBeInTheDocument();
    expect(cells.getByText("512 MB")).toBeInTheDocument();
    const docsRow = screen
      .getByRole("rowheader", { name: "docs" })
      .closest("tr");
    if (docsRow === null) throw new Error("docs has no row");
    expect(within(docsRow).getByText("none")).toBeInTheDocument();
  });

  it("opens one app's panel from Edit with focus on it, and Close hands focus back to the filter", async () => {
    const { user } = setup();
    const edit = screen.getByRole("button", { name: "Edit docs" });
    expect(edit).toHaveAttribute("aria-expanded", "false");

    await user.click(edit);
    expect(edit).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "docs" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "docs", level: 3 }),
    ).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("region", { name: "docs" })).toBeNull();
    expect(edit).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.getByRole("searchbox", { name: "Filter apps" }),
    ).toHaveFocus();
  });

  it("follows a renamed app to its new slug and keeps the outcome until it is dismissed", async () => {
    vi.mocked(updateApp).mockResolvedValue(appDetail({ slug: "sales-eu" }));
    const { user, view, rerenderInPlatform } = setup();
    await user.click(screen.getByRole("button", { name: "Edit sales" }));
    const slug = screen.getByRole("textbox", { name: "Slug" });
    await user.clear(slug);
    await user.type(slug, "sales-eu");
    await user.click(screen.getByRole("button", { name: "Save fields" }));

    // The list does not hold the new slug until the next poll.
    expect(
      await screen.findByText("Loading the renamed app…"),
    ).toBeInTheDocument();
    expect(screen.getByText("Renamed sales to sales-eu")).toBeInTheDocument();

    rerenderInPlatform(
      <AppsSection
        view={{
          ...view,
          apps: view.apps.map((app) =>
            app.slug === "sales"
              ? { ...app, slug: "sales-eu", version: 4 }
              : app,
          ),
        }}
      />,
    );
    expect(
      screen.getByRole("region", { name: "sales-eu" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Loading the renamed app…")).toBeNull();
    expect(screen.getByText("Renamed sales to sales-eu")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Renamed sales to sales-eu")).toBeNull();
  });

  it("warns when the open app leaves the list, and Close clears the warning", async () => {
    const { user, view, rerenderInPlatform } = setup();
    await user.click(screen.getByRole("button", { name: "Edit reports" }));

    rerenderInPlatform(
      <AppsSection
        view={{
          ...view,
          apps: view.apps.filter((app) => app.slug !== "reports"),
        }}
      />,
    );
    expect(screen.queryByRole("region", { name: "reports" })).toBeNull();
    expect(
      screen.getByText("That app is no longer in the list"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText("That app is no longer in the list")).toBeNull();
  });
});
