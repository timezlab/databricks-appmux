import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxIcon, LayoutGridIcon } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
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

  it("says what a count means to a screen reader, and on hover, when the item has a description", async () => {
    const user = userEvent.setup();
    render(
      <AppSidebar
        label="App sections"
        items={[
          {
            href: "/app/requests/?slug=sales",
            label: "Requests",
            icon: InboxIcon,
            count: 2,
            countDescription: "2 things need attention",
          },
        ]}
        current="/app/?slug=sales"
        backHref="/"
        backLabel="All apps"
      />,
    );
    const column = screen.getByRole("navigation", { name: "App sections" });
    const item = within(column).getByRole("link", {
      name: "Requests, 2 things need attention",
    });
    expect(within(item).getByText("2")).toHaveAttribute(
      "title",
      "2 things need attention",
    );

    await user.click(
      screen.getByRole("button", { name: "Collapse the sidebar" }),
    );
    expect(item).toHaveAttribute("title", "Requests, 2 things need attention");
    // The flag also lives in memory for this page; leave it as the next test expects.
    await user.click(
      screen.getByRole("button", { name: "Expand the sidebar" }),
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

// Below 768 px the column is hidden (CSS) and one button stands in for it;
// jsdom applies no CSS, so both are in the document here and the Chromium test
// (e2e/shell.spec.ts) checks which one shows at which width.
describe("AppSidebar, the phone menu", () => {
  function sectionButton(): HTMLElement {
    return screen.getByRole("button", { name: "Section: Requests" });
  }

  async function openMenu(user: ReturnType<typeof userEvent.setup>) {
    await user.click(sectionButton());
    return screen.findByRole("dialog", { name: "App sections" });
  }

  it("is one button that names the current section and says whether it is open", () => {
    renderSidebar();
    expect(sectionButton()).toHaveAttribute("aria-expanded", "false");
    expect(sectionButton()).toHaveTextContent("Section: Requests");
  });

  it("lists the same items, icons, counts and back link, marking the current one", async () => {
    const user = userEvent.setup();
    renderSidebar();
    const menu = await openMenu(user);

    expect(sectionButton()).toHaveAttribute("aria-expanded", "true");
    expect(
      within(menu).getByRole("link", { name: "All apps" }),
    ).toHaveAttribute("href", "/");
    // The same links as the column, in the same order.
    const hrefs = (links: HTMLElement[]) =>
      links.map((link) => link.getAttribute("href"));
    const column = screen.getByRole("navigation", { name: "App sections" });
    expect(hrefs(within(menu).getAllByRole("link"))).toEqual(
      hrefs(within(column).getAllByRole("link")),
    );
    expect(within(menu).getAllByRole("link")).toHaveLength(3);
    const requests = within(menu).getByRole("link", { name: /Requests/ });
    expect(requests).toHaveAttribute("aria-current", "page");
    expect(requests).toHaveTextContent("2");
    expect(requests.querySelector("svg")).not.toBeNull();
  });

  it("closes when an item is chosen", async () => {
    const user = userEvent.setup();
    renderSidebar();
    const menu = await openMenu(user);
    // Keep jsdom from trying to load the page; next/link has no router here.
    menu.addEventListener("click", (event) => {
      event.preventDefault();
    });

    await user.click(within(menu).getByRole("link", { name: "Overview" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(sectionButton()).toHaveAttribute("aria-expanded", "false");
  });

  it("closes on Escape and on a click outside it", async () => {
    const user = userEvent.setup();
    renderSidebar();

    await openMenu(user);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    await openMenu(user);
    await user.click(document.body);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("names the landmark instead when no item is the current page", () => {
    render(
      <AppSidebar
        label="Platform sections"
        items={[
          { href: "/platform/", label: "Overview", icon: LayoutGridIcon },
        ]}
        current="/platform/unknown/"
        backHref="/"
        backLabel="All apps"
      />,
    );
    expect(
      screen.getByRole("button", { name: "Platform sections" }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("renders on the server with no style attribute and the menu closed", () => {
    const html = renderToStaticMarkup(
      <AppSidebar
        label="App sections"
        items={[
          { href: "/app/?slug=sales", label: "Overview", icon: LayoutGridIcon },
        ]}
        current="/app/?slug=sales"
        backHref="/"
        backLabel="All apps"
      />,
    );
    expect(html).not.toMatch(/\sstyle=/);
    expect(html).toContain('aria-expanded="false"');
  });
});
