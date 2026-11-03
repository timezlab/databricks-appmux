/**
 * One geometry for every bar (docs/product/admin-ui-design.md, Layout; the
 * owner's point 8: the underline of the top navigation and of the tabs
 * differed in style, place and size). The top navigation, the tabs and the
 * sidebar draw their items with the same `bar-*` classes, so the bar has one
 * thickness, one offset and one set of colours (globals.css, `bar-item`), and
 * none of them underlines its text. The selected item carries
 * `aria-current="page"`, which is also what paints its bar.
 */
import { render, screen, within } from "@testing-library/react";
import { InboxIcon, LayoutGridIcon } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { AppSidebar } from "@/components/app-sidebar";
import { MainNav } from "@/components/main-nav";
import { SubNav } from "@/components/sub-nav";

vi.mock("next/navigation", () => ({
  usePathname: () => "/platform/",
}));

function renderThreeNavs(): void {
  render(
    <>
      <MainNav />
      <SubNav
        label="Platform sections"
        items={[
          { label: "Overview", href: "/platform/" },
          { label: "Apps", href: "/platform/apps/" },
        ]}
      />
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
        current="/app/?slug=sales"
        backHref="/"
        backLabel="All apps"
      />
    </>,
  );
}

/** The links of one navigation landmark that are its items (the sidebar's back link is not one). */
function itemLinks(navName: string): HTMLElement[] {
  const nav = screen.getByRole("navigation", { name: navName });
  return within(nav)
    .getAllByRole("link")
    .filter((link) => link.closest("li") !== null);
}

function barClasses(element: HTMLElement): string[] {
  return [...element.classList].filter((name) => name.startsWith("bar-"));
}

const NAV_NAMES = ["Main", "Platform sections", "App sections"] as const;

describe("the bar of the three navigations", () => {
  it("every item of each navigation has the same bar classes", () => {
    renderThreeNavs();
    const classSets = NAV_NAMES.flatMap((name) =>
      itemLinks(name).map((link) => barClasses(link)),
    );
    expect(classSets.length).toBeGreaterThanOrEqual(7);
    for (const classes of classSets) expect(classes).toEqual(["bar-item"]);
  });

  it("no item underlines its text, at rest or on hover", () => {
    renderThreeNavs();
    for (const name of NAV_NAMES) {
      for (const link of itemLinks(name)) {
        expect(link.className).not.toMatch(/(^|\s|:)underline(\s|$)/);
        expect(link.className).not.toMatch(/border-b/);
      }
    }
  });

  it("the selected item of each navigation, and only it, has aria-current=page", () => {
    renderThreeNavs();
    for (const [name, selected] of [
      ["Main", "Platform"],
      ["Platform sections", "Overview"],
      ["App sections", "Overview"],
    ] as const) {
      const current = itemLinks(name).filter(
        (link) => link.getAttribute("aria-current") === "page",
      );
      expect(current).toHaveLength(1);
      expect(current[0]).toHaveTextContent(selected);
    }
  });
});
