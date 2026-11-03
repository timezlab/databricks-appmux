import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

function CopyButton() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="outline" size="icon" aria-label="Copy">
          C
        </Button>
      </TooltipTrigger>
      <TooltipContent>Copy the URL</TooltipContent>
    </Tooltip>
  );
}

describe("Tooltip", () => {
  it("shows on focus and hides on Escape, without a provider of its own", async () => {
    const user = userEvent.setup();
    render(<CopyButton />);

    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.tab();

    expect(screen.getByRole("button", { name: "Copy" })).toHaveFocus();
    expect(screen.getByRole("tooltip")).toHaveTextContent("Copy the URL");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("works under a shared TooltipProvider too", async () => {
    const user = userEvent.setup();
    render(
      <TooltipProvider>
        <CopyButton />
      </TooltipProvider>,
    );

    await user.tab();
    expect(screen.getByRole("tooltip")).toHaveTextContent("Copy the URL");
  });
});
