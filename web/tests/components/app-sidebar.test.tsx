import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxIcon, LayoutGridIcon } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppSidebar, SIDEBAR_COLLAPSED_KEY } from "@/components/app-sidebar";

function renderSidebar(): void {
  render(
    <AppSidebar
      label="App sections"
      items={[
        { href: "/app/?slug=sales", label: "Overview", icon: LayoutGridIcon },
        {
          href: "/app/requests/?slug=sales",
          label: "Requests",
          icon: InboxIcon,
          count: 2,
        },
      ]}
      current="/app/requests/?slug=sales"
      backHref="/"
      backLabel="All apps"
    />,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("AppSidebar", () => {
  it("links back to the list and to each section, marking the current one", () => {
    renderSidebar();
    expect(screen.getByRole("link", { name: "All apps" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.getByRole("link", { name: /Requests/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Overview" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(screen.getByRole("link", { name: /Requests/ })).toHaveTextContent(
      "2",
    );
  });

  it("collapses to icons, keeps the labels for screen readers and remembers the choice", async () => {
    const user = userEvent.setup();
    renderSidebar();

    await user.click(
      screen.getByRole("button", { name: "Collapse the sidebar" }),
    );

    const expand = screen.getByRole("button", { name: "Expand the sidebar" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute(
      "title",
      "Overview",
    );
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe("true");

    await user.click(expand);
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe("false");
  });

  it("still collapses when the browser refuses storage", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const user = userEvent.setup();
    renderSidebar();

    await user.click(
      screen.getByRole("button", { name: "Collapse the sidebar" }),
    );

    expect(
      screen.getByRole("button", { name: "Expand the sidebar" }),
    ).toBeInTheDocument();
  });
});
