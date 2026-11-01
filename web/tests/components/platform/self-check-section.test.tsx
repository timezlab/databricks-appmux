import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { SelfCheckSection } from "@/components/platform/self-check-section";
import { ApiError, runSelfCheck } from "@/lib/api";
import type { PlatformView, SelfCheckResult } from "@/lib/api";
import { platformView, renderInPlatform } from "./shell-fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  runSelfCheck: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
});

const LEAK = "the child can read the parent's environment";

function viewWith(selfCheck: Partial<SelfCheckResult>): PlatformView {
  const view = platformView();
  return { ...view, self_check: { ...view.self_check, ...selfCheck } };
}

/** The example's checks, the first one failed with a detail. */
function failedResult(ranAt: string): SelfCheckResult {
  const { self_check } = platformView();
  return {
    ...self_check,
    passed: false,
    ran_at: ranAt,
    checks: self_check.checks.map((check, index) =>
      index === 0 ? { ...check, passed: false, detail: LEAK } : check,
    ),
  };
}

function rowOf(name: string): HTMLElement {
  const row = screen.getByRole("rowheader", { name }).closest("tr");
  if (row === null) throw new Error(`${name} has no row`);
  return row;
}

describe("SelfCheckSection states are words, not colour alone", () => {
  it("passed: the verdict and every check say Passed", () => {
    renderInPlatform(<SelfCheckSection view={platformView()} />);
    expect(screen.getByText("12 checks passed in 840 ms.")).toBeInTheDocument();
    const table = screen.getByRole("table", {
      name: "Checks of the last self-check",
    });
    expect(within(table).getAllByText("Passed")).toHaveLength(12);
    expect(within(table).queryByText("Failed")).toBeNull();
    // The verdict above the table is also a word.
    expect(screen.getAllByText("Passed")).toHaveLength(13);
    expect(screen.queryByText(/^Failed:/)).toBeNull();
  });

  it("failed: the failing check is named in a notice and marked Failed with its detail", () => {
    renderInPlatform(
      <SelfCheckSection
        view={viewWith(failedResult("2026-10-30T08:00:03Z"))}
      />,
    );
    expect(screen.getByText("Failed: environ_isolation")).toBeInTheDocument();
    expect(
      screen.getByText(
        "A failed check means an isolation guarantee may not hold.",
      ),
    ).toBeInTheDocument();
    const row = within(rowOf("environ_isolation"));
    expect(row.getByText("Failed")).toBeInTheDocument();
    expect(row.getByText(LEAK)).toBeInTheDocument();
    expect(
      within(rowOf("pid_isolation")).getByText("Passed"),
    ).toBeInTheDocument();
  });

  it("unverified: a pass with no checks is not shown as a pass", () => {
    renderInPlatform(<SelfCheckSection view={viewWith({ checks: [] })} />);
    expect(screen.getByText("Unverified")).toBeInTheDocument();
    expect(screen.getByText("Nothing was verified")).toBeInTheDocument();
    expect(screen.queryByText("Passed")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("SelfCheckSection Run again", () => {
  it("is busy while it runs, then shows and announces the new result", async () => {
    let finish: (() => void) | undefined;
    vi.mocked(runSelfCheck).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = () => {
            resolve(failedResult("2026-10-31T09:00:00Z"));
          };
        }),
    );
    const user = userEvent.setup();
    const { announce, refresh } = renderInPlatform(
      <SelfCheckSection view={platformView()} />,
    );

    await user.click(screen.getByRole("button", { name: "Run again" }));
    expect(screen.getByRole("button", { name: "Running" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    expect(runSelfCheck).toHaveBeenCalledTimes(1);

    finish?.();
    expect(
      await screen.findByText("Failed: environ_isolation"),
    ).toBeInTheDocument();
    expect(screen.getByText("Self-check failed")).toBeInTheDocument();
    expect(announce).toHaveBeenCalledWith("Self-check failed");
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Run again" }),
    ).not.toHaveAttribute("aria-busy");
  });

  it("keeps the result it ran when an older poll answer arrives afterwards", async () => {
    vi.mocked(runSelfCheck).mockResolvedValue(
      failedResult("2026-10-31T09:00:00Z"),
    );
    const user = userEvent.setup();
    const view = platformView();
    const { rerenderInPlatform } = renderInPlatform(
      <SelfCheckSection view={view} />,
    );
    await user.click(screen.getByRole("button", { name: "Run again" }));
    await screen.findByText("Failed: environ_isolation");

    rerenderInPlatform(<SelfCheckSection view={view} />);
    expect(screen.getByText("Failed: environ_isolation")).toBeInTheDocument();
  });

  it("shows why it could not run and offers Try again, which runs it again", async () => {
    vi.mocked(runSelfCheck)
      .mockRejectedValueOnce(
        new ApiError(503, "unavailable", "the canary child is starting"),
      )
      .mockResolvedValueOnce(platformView().self_check);
    const user = userEvent.setup();
    renderInPlatform(<SelfCheckSection view={platformView()} />);

    await user.click(screen.getByRole("button", { name: "Run again" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not run the self-check: appmux is not available right now",
    );
    expect(alert).toHaveTextContent("the canary child is starting");

    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Self-check passed")).toBeInTheDocument();
    expect(runSelfCheck).toHaveBeenCalledTimes(2);
  });
});
