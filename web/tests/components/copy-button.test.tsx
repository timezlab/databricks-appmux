import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "@/components/copy-button";

function renderWithTarget(): void {
  render(
    <>
      <code id="route">/sales/</code>
      <CopyButton value="/sales/" label="the route" selectTargetId="route" />
    </>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CopyButton", () => {
  it("writes the value to the clipboard and announces Copied", async () => {
    const user = userEvent.setup();
    // After setup: user-event installs a clipboard stub of its own.
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    renderWithTarget();

    await user.click(screen.getByRole("button", { name: "Copy the route" }));

    expect(writeText).toHaveBeenCalledWith("/sales/");
    expect(screen.getByRole("status")).toHaveTextContent("Copied");
  });

  it("keeps an ESC of the value off the clipboard", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(
      <CopyButton
        value={"\u001b[31mTraceback\u001b[0m\tline 1\n"}
        label="the shown lines"
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Copy the shown lines" }),
    );

    expect(writeText).toHaveBeenCalledWith("[31mTraceback[0m\tline 1\n");
  });

  it("selects the text and says how to copy it when the clipboard is refused", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.reject(new Error("denied")));
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    renderWithTarget();

    await user.click(screen.getByRole("button", { name: "Copy the route" }));

    expect(window.getSelection()?.toString()).toBe("/sales/");
    expect(screen.getByRole("status")).toHaveTextContent(
      "The text is selected",
    );
  });

  it("falls back the same way where there is no Clipboard API", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    renderWithTarget();

    await user.click(screen.getByRole("button", { name: "Copy the route" }));

    expect(window.getSelection()?.toString()).toBe("/sales/");
    expect(screen.getByRole("status")).toHaveTextContent("Could not copy");
  });
});
