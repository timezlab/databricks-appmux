import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MemorySection } from "@/components/platform/memory-section";
import { platformView, renderInPlatform } from "./shell-fixtures";

describe("MemorySection", () => {
  it("states the committed memory in words and numbers next to the strip", () => {
    renderInPlatform(<MemorySection view={platformView()} />);
    expect(
      screen.getByText("6,336 of 7,168 MB committed (88%); 832 MB free"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Memory: 1,024 MB reserved for appmux, 6,336 MB committed to apps, 832 MB free, of the 8,192 MB the container allows.",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/The container allows 8,192 MB\. appmux keeps 1,024 MB/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/The 3 registered apps hold 1,024 MB/),
    ).toBeInTheDocument();
  });

  it("names the reserved, committed and free shares under the strip with their figures", () => {
    renderInPlatform(<MemorySection view={platformView()} />);
    const expectedFigures: readonly (readonly [string, string])[] = [
      ["Reserved for appmux", "1,024 MB"],
      ["Committed to apps", "6,336 MB"],
      ["Free", "832 MB"],
    ];
    for (const [label, figure] of expectedFigures) {
      const item = screen.getByText(label).closest("li");
      expect(item).toHaveTextContent(figure);
    }
    expect(screen.getByText("88% committed")).toBeInTheDocument();
  });

  it("says the level in words once the committed share passes a threshold", () => {
    const view = platformView();
    const { rerenderInPlatform } = renderInPlatform(
      <MemorySection
        view={{
          ...view,
          memory: {
            limit_mb: 8192,
            reserve_mb: 1024,
            committed_mb: 1000,
            free_mb: 6168,
          },
        }}
      />,
    );
    expect(screen.queryByText(/Getting full|Almost full/)).toBeNull();
    rerenderInPlatform(
      <MemorySection
        view={{
          ...view,
          memory: {
            limit_mb: 8192,
            reserve_mb: 1024,
            committed_mb: 5775,
            free_mb: 1393,
          },
        }}
      />,
    );
    expect(screen.getByText("Getting full")).toBeInTheDocument();
  });

  it("says in a sentence that the slot is over-committed", () => {
    const view = platformView();
    renderInPlatform(
      <MemorySection
        view={{
          ...view,
          memory: { ...view.memory, committed_mb: 7600, free_mb: -432 },
        }}
      />,
    );
    expect(
      screen.getByText("The slot is over-committed by 432 MB."),
    ).toBeInTheDocument();
  });
});
