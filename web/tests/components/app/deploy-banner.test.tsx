import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DeployBanner } from "@/components/app/deploy-banner";
import type { DeployProgress } from "@/lib/app/deploy-progress";

function progress(overrides: Partial<DeployProgress> = {}): DeployProgress {
  return {
    releaseNo: 9,
    currentLabel: "Building",
    steps: [
      { label: "Pending", state: "done" },
      { label: "Building", state: "current" },
      { label: "Starting", state: "todo" },
      { label: "Healthy", state: "todo" },
      { label: "Active", state: "todo" },
    ],
    servingReleaseNo: 7,
    outlook: "Release 7 keeps serving until this one is healthy.",
    isBuildOutputRecorded: false,
    ...overrides,
  };
}

describe("DeployBanner", () => {
  it("tells on the Logs section that the build output is not recorded yet, and does not link to Logs", () => {
    render(<DeployBanner slug="sales" progress={progress()} section="logs" />);
    expect(
      screen.getByText(/build output is not recorded/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Read the logs" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "See the release" }),
    ).toBeInTheDocument();
  });

  it("drops that note once the new copy writes to the log", () => {
    render(
      <DeployBanner
        slug="sales"
        progress={progress({ isBuildOutputRecorded: true })}
        section="logs"
      />,
    );
    expect(
      screen.queryByText(/build output is not recorded/i),
    ).not.toBeInTheDocument();
  });

  it("keeps the note off the other sections", () => {
    render(
      <DeployBanner slug="sales" progress={progress()} section="overview" />,
    );
    expect(
      screen.queryByText(/build output is not recorded/i),
    ).not.toBeInTheDocument();
  });
});
