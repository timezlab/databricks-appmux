import { render, screen, within } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TopBar } from "@/components/top-bar";

const navigation = vi.hoisted(() => ({
  pathname: "/",
  search: "",
}));

vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useSearchParams: () => new URLSearchParams(navigation.search),
  useRouter: () => ({ push: vi.fn() }),
}));

beforeEach(() => {
  navigation.pathname = "/";
  navigation.search = "";
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TopBar", () => {
  it("holds the wordmark, the main navigation and the theme switch, and no breadcrumb outside an app", () => {
    render(<TopBar />);
    const banner = screen.getByRole("banner");
    expect(
      within(banner).getByRole("link", { name: /^appmux/ }),
    ).toHaveAttribute("href", "/");
    expect(
      within(banner).getByRole("navigation", { name: "Main" }),
    ).toBeInTheDocument();
    expect(
      within(banner).getByRole("button", { name: /^Theme: / }),
    ).toBeInTheDocument();
    expect(
      within(banner).queryByRole("button", { name: /^App: / }),
    ).not.toBeInTheDocument();
  });

  it("shows `/ <app> ▾` on the pages of one app", () => {
    navigation.pathname = "/app/requests/";
    navigation.search = "slug=unnamed";
    render(<TopBar />);
    const banner = screen.getByRole("banner");
    expect(
      within(banner).getByRole("button", { name: "App: /unnamed/" }),
    ).toBeInTheDocument();
    expect(within(banner).getByRole("link", { name: "appmux" })).toBeVisible();
  });

  it("shows no breadcrumb for a missing or invalid slug (the page says what is wrong)", () => {
    navigation.pathname = "/app/";
    for (const search of ["", "slug=", "slug=Not%20a%20slug"]) {
      navigation.search = search;
      const { unmount } = render(<TopBar />);
      expect(
        screen.queryByRole("button", { name: /^App: / }),
      ).not.toBeInTheDocument();
      unmount();
    }
  });

  it("renders on the server with no style attribute, on the launcher and on an app page", () => {
    for (const [pathname, search] of [
      ["/", ""],
      ["/app/", "slug=sales"],
      ["/platform/apps/", ""],
    ] as const) {
      navigation.pathname = pathname;
      navigation.search = search;
      const html = renderToStaticMarkup(<TopBar />);
      expect(html).not.toMatch(/\sstyle=/);
      expect(html).toContain("<header");
    }
  });
});
