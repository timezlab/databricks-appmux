import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmButton } from "@/components/confirm-button";

function setup() {
  const onConfirm = vi.fn();
  const user = userEvent.setup();
  render(
    <ConfirmButton
      label="Delete app"
      prompt="Delete sales? Its releases are removed."
      confirmLabel="Yes, delete sales"
      onConfirm={onConfirm}
    />,
  );
  return { onConfirm, user };
}

describe("ConfirmButton", () => {
  it("runs nothing on the first press and asks, with focus on Cancel", async () => {
    const { onConfirm, user } = setup();
    await user.click(screen.getByRole("button", { name: "Delete app" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(
      screen.getByRole("group", {
        name: "Delete sales? Its releases are removed.",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("calls onConfirm once, after the confirm button", async () => {
    const { onConfirm, user } = setup();
    await user.click(screen.getByRole("button", { name: "Delete app" }));
    await user.click(screen.getByRole("button", { name: "Yes, delete sales" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Delete app" }),
    ).toBeInTheDocument();
  });

  it("Escape and Cancel put the resting button back, focused, without confirming", async () => {
    const { onConfirm, user } = setup();
    await user.click(screen.getByRole("button", { name: "Delete app" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Delete app" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Delete app" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Delete app" })).toHaveFocus();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
