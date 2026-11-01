import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Notice } from "@/components/notice";

describe("Notice", () => {
  it.each([
    ["assertive", "alert"],
    ["polite", "status"],
  ] as const)("live=%s is announced as role=%s", (live, role) => {
    render(<Notice tone="danger" title="Saved" live={live} />);
    expect(screen.getByRole(role)).toHaveTextContent("Saved");
  });

  it("has no live role by default, and shows its title as text, not markup", () => {
    render(<Notice tone="info" title="<b>x</b>" />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("<b>x</b>")).toBeInTheDocument();
  });

  it("shows its children under the title", () => {
    render(
      <Notice tone="warning" title="Careful">
        <p>More words.</p>
      </Notice>,
    );
    expect(screen.getByText("Careful")).toBeInTheDocument();
    expect(screen.getByText("More words.")).toBeInTheDocument();
  });
});
