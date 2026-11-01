import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AppProblem } from "@/components/app/app-problem";

describe("AppProblem", () => {
  it("tells a visitor who may only open the app that the page is for admins, and links to the app", () => {
    render(<AppProblem kind="no-access" slug="sales" />);
    expect(
      screen.getByRole("heading", { level: 1, name: "sales" }),
    ).toBeInTheDocument();
    expect(screen.getByText("You cannot manage this app")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the app" })).toHaveAttribute(
      "href",
      "/sales/",
    );
    expect(
      screen.getByRole("link", { name: "Back to the launcher" }),
    ).toBeInTheDocument();
  });

  it("names the slug that does not exist, as text", () => {
    render(<AppProblem kind="not-found" slug="sales" />);
    expect(screen.getByText("There is no such app")).toBeInTheDocument();
    expect(screen.getByText("sales")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Open the app" }),
    ).not.toBeInTheDocument();
  });

  it("shows an address value that cannot be a slug as plain text, never as a link or markup", () => {
    const value = '<a href="javascript:alert(1)">x</a>';
    const { container } = render(<AppProblem kind="invalid" value={value} />);
    expect(screen.getByText(value)).toBeInTheDocument();
    expect(container.querySelector('a[href^="javascript"]')).toBeNull();
    expect(
      screen.getByRole("link", { name: "Back to the launcher" }),
    ).toBeInTheDocument();
  });

  it("asks for a choice when the address names no app", () => {
    render(<AppProblem kind="missing" />);
    expect(screen.getByText("No app chosen")).toBeInTheDocument();
  });
});
