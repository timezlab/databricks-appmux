import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

describe("ToggleGroup", () => {
  it("moves between items with the arrow keys once the browser has taken over, and picks one", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(
      <ToggleGroup
        type="single"
        defaultValue="1h"
        aria-label="Range"
        onValueChange={onValueChange}
      >
        <ToggleGroupItem value="15m">15 min</ToggleGroupItem>
        <ToggleGroupItem value="1h">1 hour</ToggleGroupItem>
        <ToggleGroupItem value="24h">24 hours</ToggleGroupItem>
      </ToggleGroup>,
    );

    await user.tab();
    expect(screen.getByRole("radio", { name: "1 hour" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "24 hours" })).toHaveFocus();
    await user.keyboard(" ");

    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("24h");
    expect(screen.getByRole("radio", { name: "24 hours" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});
