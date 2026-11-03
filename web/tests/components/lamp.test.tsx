import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Lamp, lampStateOfTone } from "@/components/lamp";
import { AppStateBadge } from "@/components/status-badge";

describe("lampStateOfTone", () => {
  it("draws each tone of a status as the lamp of the same meaning", () => {
    expect(lampStateOfTone("success")).toBe("running");
    expect(lampStateOfTone("info")).toBe("starting");
    expect(lampStateOfTone("neutral")).toBe("stopped");
    expect(lampStateOfTone("warning")).toBe("warning");
    expect(lampStateOfTone("danger")).toBe("failed");
  });
});

describe("Lamp", () => {
  it("always shows its word, with the sentence about it as the hover title", () => {
    render(<Lamp state="failed" label="Failed" description="Did not start." />);
    const lamp = screen.getByText("Failed");
    expect(lamp).toHaveAttribute("title", "Did not start.");
    expect(lamp).toHaveAttribute("data-lamp", "failed");
  });

  it("breathes only while starting", () => {
    const { container } = render(
      <>
        <Lamp state="starting" label="Starting" />
        <Lamp state="running" label="Running" />
      </>,
    );
    const breathing = container.querySelectorAll(".animate-lamp-breathe");
    expect(breathing).toHaveLength(1);
    expect(breathing[0]?.closest("[data-lamp]")).toHaveAttribute(
      "data-lamp",
      "starting",
    );
  });
});

describe("AppStateBadge", () => {
  it("draws an app state as a lamp with its word", () => {
    render(<AppStateBadge state="crashed" />);
    expect(screen.getByText("Crashed")).toHaveAttribute("data-lamp", "failed");
  });
});
