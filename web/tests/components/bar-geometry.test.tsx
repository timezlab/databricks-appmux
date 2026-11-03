/**
 * One geometry for every bar (docs/product/admin-ui-design.md, Layout; the
 * owner's point 8: the underline of the top navigation and of the tabs
 * differed in style, place and size). The bar lies on the item's edge, so the
 * item's padding is the gap between the label and the bar: the padding and the
 * bar both live in the `bar-item` utility of globals.css, and a vertical list
 * (the sidebar) adapts them with `bar-axis-vertical` on its container. These
 * tests render the real consumers (the top navigation, the Environment page's
 * tab bar and the sidebar) and fail when an item drops `bar-item`, sets a
 * padding of its own, or underlines its text. A padding of its own is any
 * padding utility on the item whatever its variants (`data-[state=active]:py-3`,
 * `!py-3`), or one that an element between the item and the bar's container
 * (the landmark or the tablist), the container included, gives the items
 * inside it (`[&_a]:py-3`, `*:px-2`). Horizontal padding counts too: in the
 * sidebar the bar lies on the start edge, so `px` is the gap there, and
 * `bar-item` sets both.
 */
import { render, screen, within } from "@testing-library/react";
import { InboxIcon, LayoutGridIcon } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppSidebar } from "@/components/app-sidebar";
import { EnvironmentPanel } from "@/components/app/environment/environment-panel";
import { MainNav } from "@/components/main-nav";
import { readExample } from "../support/contract-examples";
import { jsonResponse, stubFetch } from "../support/fake-fetch";

vi.mock("next/navigation", () => ({
  usePathname: () => "/platform/",
}));

afterEach(() => {
  vi.restoreAllMocks();
});

const TAB_BAR_NAME = "What the app starts with";

async function renderTheBars(): Promise<void> {
  stubFetch(() => jsonResponse(200, readExample("AppEnvironment")));
  render(
    <>
      <MainNav />
      <EnvironmentPanel slug="sales" />
      <AppSidebar
        label="App sections"
        items={[
          { href: "/app/?slug=sales", label: "Overview", icon: LayoutGridIcon },
          {
            href: "/app/requests/?slug=sales",
            label: "Requests",
            icon: InboxIcon,
            count: 2,
            countDescription: "2 waiting",
          },
        ]}
        current="/app/?slug=sales"
        backHref="/"
        backLabel="All apps"
      />
    </>,
  );
  await screen.findByRole("tablist", { name: TAB_BAR_NAME });
}

/** The links of one navigation landmark that are its items (the sidebar's back link is not one). */
function navItems(navName: string): HTMLElement[] {
  const nav = screen.getByRole("navigation", { name: navName });
  return within(nav)
    .getAllByRole("link")
    .filter((link) => link.closest("li") !== null);
}

function tabItems(): HTMLElement[] {
  return within(
    screen.getByRole("tablist", { name: TAB_BAR_NAME }),
  ).getAllByRole("tab");
}

/**
 * Every bar's container (the element with the landmark or the tablist role)
 * and items, by the bar's name, with the attribute that marks the chosen one
 * and its text.
 */
function bars(): {
  name: string;
  container: HTMLElement;
  items: HTMLElement[];
  chosenBy: "aria-current" | "aria-selected";
  chosenText: string;
}[] {
  return [
    {
      name: "Main",
      container: screen.getByRole("navigation", { name: "Main" }),
      items: navItems("Main"),
      chosenBy: "aria-current",
      chosenText: "Platform",
    },
    {
      name: "tab bar",
      container: screen.getByRole("tablist", { name: TAB_BAR_NAME }),
      items: tabItems(),
      chosenBy: "aria-selected",
      chosenText: "Variables",
    },
    {
      name: "App sections",
      container: screen.getByRole("navigation", { name: "App sections" }),
      items: navItems("App sections"),
      chosenBy: "aria-current",
      chosenText: "Overview",
    },
  ];
}

/** The elements from the item's parent up to the bar's container, both included. */
function ancestorsUpTo(
  item: HTMLElement,
  container: HTMLElement,
): HTMLElement[] {
  const ancestors: HTMLElement[] = [];
  let element = item.parentElement;
  while (element !== null) {
    ancestors.push(element);
    if (element === container) return ancestors;
    element = element.parentElement;
  }
  throw new Error("the item is not inside its bar's container");
}

function barClasses(element: HTMLElement): string[] {
  return [...element.classList].filter((name) => name.startsWith("bar-"));
}

/**
 * A class split at its top-level colons into its variants and its utility,
 * the `!` of an important class (before or after the utility) dropped. A colon
 * inside brackets or parentheses belongs to an arbitrary variant
 * (`supports-[display:grid]:`, `[&:hover]:`), not to the split.
 */
function parseClass(name: string): { variants: string[]; utility: string } {
  const variants: string[] = [];
  let depth = 0;
  let start = 0;
  for (let index = 0; index < name.length; index += 1) {
    const character = name[index];
    if (character === "[" || character === "(") depth += 1;
    else if (character === "]" || character === ")") depth -= 1;
    else if (character === ":" && depth === 0) {
      variants.push(name.slice(start, index));
      start = index + 1;
    }
  }
  return { variants, utility: name.slice(start).replace(/^!|!$/g, "") };
}

/** A padding utility on any side (`py-3`, `pe-2`, `p-0`), or an arbitrary padding property (`[padding-block:3px]`). */
const PADDING_UTILITY = /^(?:p[xytblrse]?-|\[padding[\w-]*:)/;

/**
 * A variant that styles the element's children or descendants, not the
 * element: `*:` and `**:`, or an arbitrary selector with a combinator
 * (`[&_a]:`, `[&>li]:`; Tailwind writes the space as `_`).
 */
function isDescendantVariant(variant: string): boolean {
  return (
    variant === "*" ||
    variant === "**" ||
    (variant.startsWith("[") && /[_\s>~+]/.test(variant))
  );
}

/** A padding on the item, whatever its variants: `py-3`, `md:px-2`, `data-[state=active]:py-3`, `!py-3`. */
function isPaddingClass(name: string): boolean {
  return PADDING_UTILITY.test(parseClass(name).utility);
}

/** A padding a container (the list, the landmark) gives the items inside it: `[&_a]:py-3`, `*:px-2`. */
function isDescendantPaddingClass(name: string): boolean {
  const { variants, utility } = parseClass(name);
  return PADDING_UTILITY.test(utility) && variants.some(isDescendantVariant);
}

describe("the padding matchers of these tests", () => {
  it.each([
    "py-3",
    "px-3",
    "pb-1",
    "p-0",
    "md:py-3",
    "max-md:pe-2",
    "data-[state=active]:py-3",
    "aria-[current=page]:py-3",
    "group-data-[x]:pt-2",
    "supports-[display:grid]:py-2",
    "!py-3",
    "py-3!",
    "md:!pt-1",
    "[&_a]:py-3",
    "[padding-block:3px]",
  ])("take %s for a padding of the item", (name) => {
    expect(isPaddingClass(name)).toBe(true);
  });

  it.each([
    "bar-item",
    "text-sm",
    "data-[state=active]:text-sm",
    "-mb-px",
    "pointer-events-none",
    "[&_svg]:pointer-events-none",
    "[&_svg:not([class*='size-'])]:size-4",
    "whitespace-nowrap",
    "place-items-center",
    "space-y-2",
    "scroll-py-2",
  ])("do not take %s for a padding", (name) => {
    expect(isPaddingClass(name)).toBe(false);
  });

  it.each([
    "[&_a]:py-3",
    "[&>li]:px-2",
    "[&_li>a]:pt-1",
    "*:py-3",
    "**:pb-2",
    "md:[&_a]:py-3",
    "[&_a]:!py-3",
  ])("take %s on a container for a padding of the items inside it", (name) => {
    expect(isDescendantPaddingClass(name)).toBe(true);
  });

  it.each([
    "py-3",
    "md:px-4",
    "p-1",
    "data-[state=active]:py-3",
    "[&:hover]:py-3",
    "[&_a]:text-sm",
    "-mb-px",
  ])("leave %s on a container to the container itself", (name) => {
    expect(isDescendantPaddingClass(name)).toBe(false);
  });
});

describe("the bar of the top navigation, the tab bar and the sidebar", () => {
  it("every item of each bar has the one bar class, and the sidebar's list its axis", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      expect(bar.items.length, bar.name).toBeGreaterThanOrEqual(2);
      for (const item of bar.items) {
        expect(barClasses(item), `${bar.name}: ${item.textContent}`).toEqual([
          "bar-item",
        ]);
      }
    }
    for (const item of navItems("App sections")) {
      expect(item.closest("ul")).toHaveClass("bar-axis-vertical");
    }
  });

  it("no item sets a padding of its own: the gap between label and bar is bar-item's", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      for (const item of bar.items) {
        expect(
          [...item.classList].filter(isPaddingClass),
          `${bar.name}: ${item.textContent}`,
        ).toEqual([]);
      }
    }
  });

  it("no container up to the bar's landmark or tablist pads the items inside it", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      for (const item of bar.items) {
        const padding = ancestorsUpTo(item, bar.container).flatMap((element) =>
          [...element.classList].filter(isDescendantPaddingClass),
        );
        expect(padding, `${bar.name}: ${item.textContent}`).toEqual([]);
      }
    }
  });

  it("no item underlines its text, at rest or on hover", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      for (const item of bar.items) {
        expect(item.className).not.toMatch(/(^|\s|:)underline(\s|$)/);
        expect(item.className).not.toMatch(/border-b/);
      }
    }
  });

  it("the chosen item of each bar, and only it, is marked as chosen", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      const chosen = bar.items.filter((item) =>
        bar.chosenBy === "aria-current"
          ? item.getAttribute("aria-current") === "page"
          : item.getAttribute("aria-selected") === "true",
      );
      expect(chosen, bar.name).toHaveLength(1);
      expect(chosen[0]).toHaveTextContent(bar.chosenText);
    }
  });
});
