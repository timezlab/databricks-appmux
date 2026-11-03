import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as Api from "@/lib/api";
import { AppDetail, ReleaseDetail } from "@/lib/api";
import { REASON_NOT_MANAGER } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import type { WriteResult } from "@/lib/app/writes";
import { AppOverview } from "@/components/app/app-header";
import type { AppActions } from "@/components/app/use-app-actions";
import { readExample } from "../../support/contract-examples";

const { getRelease, restartApp } = vi.hoisted(() => ({
  getRelease: vi.fn(),
  restartApp: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof Api>()),
  getRelease,
  restartApp,
}));

/** The example: running, release 7 active, release 8 the failed target. */
const EXAMPLE_APP = AppDetail.parse(readExample("AppDetail"));
const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const VISITOR: Actor = { ...MANAGER, permission: "CAN_USE", canManage: false };

const NODE_HINT =
  "The memory budget (64 MB) is below what the node runtime needs to start; about 768 MB is enough for most apps.";

const actions: AppActions = {
  stateOf: () => "idle",
  run: async <T,>(key: string, call: () => Promise<T>) => {
    try {
      return { ok: true, value: await call() } satisfies WriteResult<T>;
    } catch (error) {
      return { ok: false, error } satisfies WriteResult<T>;
    }
  },
};

function setup(app: AppDetail = EXAMPLE_APP, actor: Actor | null = MANAGER) {
  const user = userEvent.setup();
  render(
    <AppOverview
      app={app}
      actor={actor}
      actions={actions}
      onReload={vi.fn()}
    />,
  );
  return { user };
}

function controls(): HTMLElement {
  return screen.getByRole("group", { name: "App controls" });
}

function attention(): HTMLElement {
  return screen.getByRole("list", { name: "Needs attention" });
}

beforeEach(() => {
  restartApp.mockReset();
  getRelease.mockReset().mockResolvedValue(
    ReleaseDetail.parse({
      ...(readExample("ReleaseDetail") as object),
      release_no: 8,
      status: "failed",
      is_target: true,
      failure_hint: NODE_HINT,
    }),
  );
});

describe("AppOverview", () => {
  it("offers Start, Stop, Restart and Update in one row, and says why a button is off", () => {
    setup();
    const row = controls();
    expect(within(row).getByRole("button", { name: "Start" })).toBeDisabled();
    for (const name of ["Stop", "Restart", "Update"]) {
      expect(within(row).getByRole("button", { name })).toBeEnabled();
    }
    expect(
      screen.getByText(
        "Start: The app is already meant to run; use Restart to start it again.",
      ),
    ).toBeInTheDocument();
  });

  it("turns every button off for a person who cannot manage the app, with one reason", () => {
    setup(EXAMPLE_APP, VISITOR);
    for (const button of within(controls()).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    expect(
      screen.getByText(
        `Start, Stop, Restart and Update: ${REASON_NOT_MANAGER}`,
      ),
    ).toBeInTheDocument();
  });

  it("lists the failed target under Needs attention with its reason, the server's hint and the way to fix each", async () => {
    setup();
    const [item] = within(attention()).getAllByRole("listitem");
    if (item === undefined) throw new Error("no notice");
    expect(item).toHaveTextContent("Release 8 failed");
    expect(item).toHaveTextContent("health check did not pass within 60 s");
    expect(item).toHaveTextContent("Release 7 keeps serving.");
    expect(
      within(item).getByRole("link", { name: "See release 8" }),
    ).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/app\/deployments\/?\?slug=sales$/),
    );

    expect(await within(item).findByText(NODE_HINT)).toBeInTheDocument();
    expect(
      within(item).getByRole("link", { name: "Change the memory budget" }),
    ).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/app\/settings\/?\?slug=sales$/),
    );
    expect(getRelease).toHaveBeenCalledExactlyOnceWith(
      "sales",
      8,
      expect.anything(),
    );
  });

  it("says nothing needs attention for a healthy app, and reads no release", () => {
    const healthy = AppDetail.parse({
      ...EXAMPLE_APP,
      releases: EXAMPLE_APP.releases.filter(
        (release) => release.release_no !== 8,
      ),
    });
    setup(healthy);
    expect(
      screen.getByText(
        "Nothing needs attention: the app runs the release it should.",
      ),
    ).toBeInTheDocument();
    expect(getRelease).not.toHaveBeenCalled();
  });

  it("restarts from the notice of changed resources, after asking", async () => {
    restartApp.mockResolvedValue(undefined);
    const stale = AppDetail.parse({
      ...EXAMPLE_APP,
      restart_to_apply: true,
    });
    const { user } = setup(stale);
    const notice = within(attention())
      .getAllByRole("listitem")
      .find((item) => item.textContent.includes("Restart to apply"));
    if (notice === undefined) throw new Error("no restart notice");

    await user.click(
      within(notice).getByRole("button", { name: "Restart now" }),
    );
    expect(restartApp).not.toHaveBeenCalled();
    await user.click(
      within(notice).getByRole("button", { name: "Yes, restart sales" }),
    );
    expect(restartApp).toHaveBeenCalledExactlyOnceWith("sales");
  });

  it("states the facts of the app, with its owner and the active release", () => {
    setup();
    const list = screen.getByText("Owner").closest("dl");
    if (list === null) throw new Error("no fact list");
    expect(list).toHaveTextContent("Active releaseRelease 7, fix pagination");
    expect(list).toHaveTextContent("Owneralice@example.com");
    expect(list).toHaveTextContent("Memory budget");
    expect(list).toHaveTextContent("/Workspace/Shared/apps/sales");
  });
});
