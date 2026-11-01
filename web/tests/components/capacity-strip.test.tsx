import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CapacityStrip } from "@/components/capacity-strip";

const COMMITTED = {
  key: "committed",
  label: "Committed",
  fill: "used",
  valueText: "50 MB",
} as const;

describe("CapacityStrip", () => {
  it("draws twenty square cells and counts the fill in them", () => {
    render(
      <CapacityStrip
        label="Memory 50%"
        total={100}
        segments={[{ ...COMMITTED, amount: 50 }]}
        hasLegend={false}
      />,
    );
    const strip = screen.getByRole("img", { name: "Memory 50%" });
    expect(strip.children).toHaveLength(20);
    expect(
      [...strip.children].filter((cell) =>
        cell.className.includes("bg-foreground"),
      ),
    ).toHaveLength(10);
    expect(
      [...strip.children].filter((cell) =>
        cell.className.includes("bg-accent"),
      ),
    ).toHaveLength(10);
  });

  it("hides the cells from a screen reader and writes no style attribute", () => {
    const { container } = render(
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[{ ...COMMITTED, amount: 50 }]}
      />,
    );
    const strip = screen.getByRole("img", { name: "Memory" });
    expect(
      [...strip.children].every(
        (cell) => cell.getAttribute("aria-hidden") === "true",
      ),
    ).toBe(true);
    expect(container.querySelector("[style]")).toBeNull();
  });

  it("uses the warning and danger fills only when the segment asks for them, and says the level in words", () => {
    render(
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[{ ...COMMITTED, amount: 95, fill: "danger" }]}
        figure="95% committed"
        level="danger"
        levelText="Almost full"
      />,
    );
    const strip = screen.getByRole("img", { name: "Memory" });
    expect(strip.children[0]?.className).toContain("bg-tone-danger-border");
    expect(screen.getByText("95% committed")).toBeInTheDocument();
    expect(screen.getByText("Almost full")).toBeInTheDocument();
  });

  it("stays in neutral ink with no level text while normal", () => {
    render(
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[{ ...COMMITTED, amount: 20 }]}
        figure="20% committed"
        levelText="Getting full"
        level="normal"
      />,
    );
    const strip = screen.getByRole("img", { name: "Memory" });
    expect(strip.innerHTML).not.toContain("tone-");
    expect(screen.queryByText("Getting full")).toBeNull();
  });

  it("lists every segment in a legend with its figure", () => {
    render(
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[
          { ...COMMITTED, amount: 50 },
          {
            key: "free",
            label: "Free",
            amount: 50,
            fill: "free",
            valueText: "50 MB",
          },
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "Committed50 MB",
      "Free50 MB",
    ]);
  });
});
