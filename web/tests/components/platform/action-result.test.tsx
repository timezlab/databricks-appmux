import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ActionResult } from "@/components/platform/action-result";
import { PlatformProvider } from "@/components/platform/platform-context";
import { ApiError } from "@/lib/api";
import type { ActionState } from "@/lib/platform/action";

function setup(state: ActionState) {
  const refresh = vi.fn();
  const retry = vi.fn();
  const user = userEvent.setup();
  const view = render(
    <PlatformProvider value={{ refresh, announce: vi.fn() }}>
      <ActionResult
        action={{ state, retry }}
        context="Could not save the app"
      />
    </PlatformProvider>,
  );
  return { user, refresh, retry, ...view };
}

describe("ActionResult", () => {
  it.each<ActionState>([{ status: "idle" }, { status: "busy" }])(
    "shows nothing while the write is $status",
    (state) => {
      const { container } = setup(state);
      expect(container).toBeEmptyDOMElement();
    },
  );

  it("shows the outcome and its detail without announcing it again (the page's live region does)", () => {
    setup({
      status: "done",
      outcome: {
        tone: "success",
        title: "Saved sales",
        detail: "It restarts.",
      },
    });
    expect(screen.getByText("Saved sales")).toBeInTheDocument();
    expect(screen.getByText("It restarts.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("maps a stale version (409) to the conflict message and a Reload of the page's data", async () => {
    const { user, refresh, retry } = setup({
      status: "failed",
      error: new ApiError(
        409,
        "changed_by_others",
        "The app was changed by someone else",
      ),
    });
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not save the app: Changed by someone else",
    );
    expect(alert).toHaveTextContent(
      "Reload to see the current state, then retry.",
    );
    await user.click(within(alert).getByRole("button", { name: "Reload" }));
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(retry).not.toHaveBeenCalled();
  });

  it("maps a lost connection to Try again, which repeats the write", async () => {
    const { user, refresh, retry } = setup({
      status: "failed",
      error: new ApiError(0, "network_error", "Failed to fetch"),
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not save the app: Could not reach appmux",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("offers no button for a refusal that retrying cannot fix", () => {
    setup({
      status: "failed",
      error: new ApiError(
        403,
        "forbidden",
        "Only a platform admin may do this",
      ),
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "You do not have permission",
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});
