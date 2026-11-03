import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RouteStrip } from "@/components/route-strip";

describe("RouteStrip", () => {
  it("shows the app's route, not a link, without an href", () => {
    render(<RouteStrip slug="sales" />);
    expect(screen.getByText("/sales/")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("is a plain link to the href it is given", () => {
    render(<RouteStrip slug="sales" href="/sales/" />);
    expect(screen.getByRole("link", { name: "/sales/" })).toHaveAttribute(
      "href",
      "/sales/",
    );
  });
});
