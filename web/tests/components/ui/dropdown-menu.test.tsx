import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

function ActionsMenu({
  onRestart,
  onDelete,
}: {
  onRestart: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline">Actions</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuLabel>sales</DropdownMenuLabel>
          <DropdownMenuItem onSelect={onRestart}>Restart</DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={onDelete}>
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <button type="button">Elsewhere</button>
    </>
  );
}

function trigger(): HTMLElement {
  return screen.getByRole("button", { name: "Actions" });
}

describe("DropdownMenu", () => {
  it.each([
    ["Enter", "{Enter}"],
    ["Space", " "],
    ["ArrowDown", "{ArrowDown}"],
  ])(
    "opens with %s on the first item, and an item runs onSelect",
    async (_key, keys) => {
      const user = userEvent.setup();
      const onRestart = vi.fn();
      const onDelete = vi.fn();
      render(<ActionsMenu onRestart={onRestart} onDelete={onDelete} />);

      trigger().focus();
      await user.keyboard(keys);

      expect(screen.getByRole("menu")).toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Restart" })).toHaveFocus();

      await user.keyboard("{ArrowDown}{Enter}");

      expect(onDelete).toHaveBeenCalledOnce();
      expect(onRestart).not.toHaveBeenCalled();
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
      expect(trigger()).toHaveFocus();
    },
  );

  it("is not modal: the page around it stays reachable", async () => {
    const user = userEvent.setup();
    render(<ActionsMenu onRestart={vi.fn()} onDelete={vi.fn()} />);

    trigger().focus();
    await user.keyboard("{Enter}");

    expect(screen.getByRole("menu")).toBeInTheDocument();
    // A modal menu hides the rest from assistive technology and turns pointer
    // events off on the body (and locks scrolling with an injected <style>).
    expect(
      screen.getByRole("button", { name: "Elsewhere", hidden: false }),
    ).not.toHaveAttribute("aria-hidden");
    expect(document.body.style.pointerEvents).toBe("");
    expect(document.querySelectorAll("style")).toHaveLength(0);
  });

  it("closes on Escape with focus back on the trigger", async () => {
    const user = userEvent.setup();
    const onRestart = vi.fn();
    render(<ActionsMenu onRestart={onRestart} onDelete={vi.fn()} />);

    await user.click(trigger());
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(onRestart).not.toHaveBeenCalled();
  });
});
