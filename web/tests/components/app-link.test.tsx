import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AppLink } from "@/components/app-link";

describe("AppLink", () => {
  it("is one plain link to the child's own route", () => {
    render(<AppLink slug="sales">Open sales</AppLink>);
    expect(screen.getByRole("link", { name: "Open sales" })).toHaveAttribute(
      "href",
      "/sales/",
    );
  });

  it("drawn as a button it is still a single link, not a button inside a link", () => {
    const { container } = render(
      <AppLink slug="sales" buttonVariant="primary">
        Open sales
      </AppLink>,
    );
    expect(screen.getByRole("link", { name: "Open sales" })).toHaveAttribute(
      "href",
      "/sales/",
    );
    expect(container.querySelector("button")).toBeNull();
  });

  it.each(["../etc", "_appmux", "Sales/../x", ""])(
    "shows a slug that fails the contract (%j) as text with no link",
    (slug) => {
      render(<AppLink slug={slug}>Open</AppLink>);
      expect(screen.queryByRole("link")).toBeNull();
      expect(screen.getByText("Open")).toBeInTheDocument();
    },
  );
});
