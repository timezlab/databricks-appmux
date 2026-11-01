import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button } from "@/components/button";

describe("Button", () => {
  it("is type=button unless told otherwise, so it never submits a form by accident", () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute(
      "type",
      "button",
    );
  });

  it("ignores clicks while busy, stays focusable, and shows the busy label", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <Button isBusy busyLabel="Saving" onClick={onClick}>
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Saving" });
    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("aria-disabled", "true");
  });

  it("does not submit its form while busy", async () => {
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => {
      event.preventDefault();
    });
    const user = userEvent.setup();
    render(
      <form onSubmit={onSubmit}>
        <Button type="submit" isBusy>
          Save
        </Button>
      </form>,
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
