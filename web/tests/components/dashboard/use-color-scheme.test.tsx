import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useColorScheme } from "@/components/dashboard/use-color-scheme";
import { fakeMatchMedia } from "./match-media";

const media = fakeMatchMedia();

function Probe() {
  return <p data-testid="scheme">{useColorScheme()}</p>;
}

function scheme(): string | null {
  return screen.getByTestId("scheme").textContent;
}

afterEach(() => {
  delete document.documentElement.dataset.theme;
  media.setPrefersDark(false);
});

describe("useColorScheme", () => {
  it("follows the operating system while no theme is chosen", () => {
    render(<Probe />);
    expect(scheme()).toBe("light");

    act(() => {
      media.setPrefersDark(true);
    });
    expect(scheme()).toBe("dark");
  });

  it("follows the theme chosen on <html>, over the operating system", async () => {
    render(<Probe />);
    expect(scheme()).toBe("light");

    // A MutationObserver reports after the change, in a microtask.
    await act(async () => {
      document.documentElement.dataset.theme = "dark";
      await Promise.resolve();
    });
    expect(scheme()).toBe("dark");

    await act(async () => {
      document.documentElement.dataset.theme = "light";
      media.setPrefersDark(true);
      await Promise.resolve();
    });
    expect(scheme()).toBe("light");
  });

  it("goes back to the operating system when the choice is removed", async () => {
    document.documentElement.dataset.theme = "light";
    media.setPrefersDark(true);
    render(<Probe />);
    expect(scheme()).toBe("light");

    await act(async () => {
      delete document.documentElement.dataset.theme;
      await Promise.resolve();
    });
    expect(scheme()).toBe("dark");
  });
});
