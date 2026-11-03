import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as Api from "@/lib/api";
import {
  ApiError,
  AppDetail,
  ReleaseDetail,
  ReleaseSourceListing,
} from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import type { WriteResult } from "@/lib/app/writes";
import { ReleaseSection } from "@/components/app/release-section";
import type { AppActions } from "@/components/app/use-app-actions";
import { readExample } from "../../support/contract-examples";

const { deleteRelease, getRelease, listReleaseSource, lockRelease } =
  vi.hoisted(() => ({
    deleteRelease: vi.fn(),
    getRelease: vi.fn(),
    listReleaseSource: vi.fn(),
    lockRelease: vi.fn(),
  }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof Api>()),
  deleteRelease,
  getRelease,
  listReleaseSource,
  lockRelease,
}));

const app = AppDetail.parse(readExample("AppDetail"));
const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};

const NODE_HINT =
  "The memory budget (64 MB) is below what the node runtime needs to start; about 768 MB is enough for most apps.";
const NODE_OUTPUT = "node: --max-old-space-size: out of memory\nexit 134";

/** Release 8 of the example, the failed target, as `GET /releases/8` gives it. */
const FAILED_TARGET = ReleaseDetail.parse({
  ...(readExample("ReleaseDetail") as object),
  release_no: 8,
  status: "failed",
  status_reason: "health check did not pass within 60 s",
  name: null,
  tags: [],
  is_target: true,
  build_log_tail: NODE_OUTPUT,
  failure_hint: NODE_HINT,
});

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

function setup() {
  const onReload = vi.fn();
  const user = userEvent.setup();
  render(
    <ReleaseSection
      app={app}
      actor={MANAGER}
      actions={actions}
      onReload={onReload}
    />,
  );
  return { onReload, user };
}

/** The table row of release `no`: its row header reads "#6" or "#6, name". */
function rowOf(no: number): HTMLElement {
  const header = screen
    .getAllByRole("rowheader")
    .find((cell) => cell.textContent.startsWith(`#${String(no)}`));
  const row = header?.closest("tr");
  if (!row) throw new Error(`no row for release ${String(no)}`);
  return row;
}

function triggerOf(no: number): HTMLElement {
  return within(rowOf(no)).getByRole("button", {
    name: `Actions for release ${String(no)}`,
  });
}

async function choose(
  user: ReturnType<typeof userEvent.setup>,
  no: number,
  name: RegExp,
): Promise<void> {
  await user.click(triggerOf(no));
  await user.click(
    within(screen.getByRole("menu")).getByRole("menuitem", { name }),
  );
}

beforeEach(() => {
  deleteRelease.mockReset();
  getRelease.mockReset().mockResolvedValue(FAILED_TARGET);
  listReleaseSource.mockReset();
  lockRelease.mockReset();
});

describe("ReleaseSection", () => {
  it("lists the releases newest first under Deployments, each row with its own menu", () => {
    setup();
    expect(
      screen.getByRole("heading", { level: 2, name: "Deployments" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Update" })).toBeEnabled();
    const table = screen.getByRole("table", {
      name: "Releases of this app, newest first",
    });
    const numbers = within(table)
      .getAllByRole("rowheader")
      .map((cell) => /^#(\d+)/.exec(cell.textContent)?.[1]);
    expect(numbers).toEqual(["8", "7", "6", "5"]);
    expect(triggerOf(6)).toBeEnabled();
    expect(rowOf(5)).toHaveTextContent("Locked by alice@example.com");
  });

  it("explains the failed target on its row: the reason, the hint with a way to Settings, and the output", async () => {
    setup();
    const row = rowOf(8);
    expect(row).toHaveTextContent("health check did not pass within 60 s");

    expect(await within(row).findByText(NODE_HINT)).toBeInTheDocument();
    expect(
      within(row).getByRole("link", { name: "Change the memory budget" }),
    ).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/app\/settings\/?\?slug=sales$/),
    );
    expect(
      within(row).getByRole("region", { name: "Output of release 8" }),
    ).toHaveTextContent(/out of memory/);
    expect(
      within(row).getByRole("button", {
        name: /^Copy the output of release 8/,
      }),
    ).toBeInTheDocument();
    expect(getRelease).toHaveBeenCalledWith("sales", 8, expect.anything());
    // Only the failed target is read on its own: no call per row.
    expect(getRelease).toHaveBeenCalledTimes(1);
  });

  it("asks under the table before deleting, and deletes only the release of the row", async () => {
    deleteRelease.mockResolvedValue(undefined);
    const { user } = setup();
    await choose(user, 6, /^Delete/);
    expect(deleteRelease).not.toHaveBeenCalled();

    const question = screen.getByRole("group", { name: /^Delete release 6\?/ });
    expect(
      within(question).getByRole("button", { name: "Cancel" }),
    ).toHaveFocus();
    await user.click(
      within(question).getByRole("button", { name: "Yes, delete release 6" }),
    );
    expect(deleteRelease).toHaveBeenCalledExactlyOnceWith("sales", 6);
  });

  it("deletes nothing on Escape, and gives the keyboard back to the row's menu", async () => {
    const { user } = setup();
    await choose(user, 6, /^Delete/);
    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("group", { name: /^Delete release 6\?/ }),
    ).not.toBeInTheDocument();
    expect(triggerOf(6)).toHaveFocus();
    expect(deleteRelease).not.toHaveBeenCalled();
  });

  it("shows why a write failed under the table, with a reload after a 409", async () => {
    lockRelease.mockRejectedValue(
      new ApiError(409, "changed_by_others", "release 6 changed"),
    );
    const { onReload, user } = setup();
    await choose(user, 6, /^Lock/);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not lock release 6",
    );
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("opens the edit form on the field that was chosen, and Cancel returns to the menu", async () => {
    const { user } = setup();
    await choose(user, 6, /^Rename/);
    const form = screen.getByRole("form", { name: "Edit release 6" });
    expect(within(form).getByRole("textbox", { name: /^Name/ })).toHaveFocus();

    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("form", { name: "Edit release 6" }),
    ).not.toBeInTheDocument();
    expect(triggerOf(6)).toHaveFocus();

    await choose(user, 6, /^Tag/);
    expect(
      within(screen.getByRole("form", { name: "Edit release 6" })).getByRole(
        "textbox",
        { name: /^Tags/ },
      ),
    ).toHaveFocus();
  });

  it("reads the source listing only when View source is chosen, and Close returns to the menu", async () => {
    listReleaseSource.mockResolvedValue(
      ReleaseSourceListing.parse(readExample("ReleaseSourceListing")),
    );
    const { user } = setup();
    expect(listReleaseSource).not.toHaveBeenCalled();

    await choose(user, 7, /^View source/);
    expect(
      await screen.findByRole("region", {
        name: "Source of release 7 of sales",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Source of release 7" }),
    ).toHaveFocus();
    expect(listReleaseSource).toHaveBeenCalledExactlyOnceWith("sales", 7);

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen.queryByRole("region", { name: "Source of release 7 of sales" }),
    ).not.toBeInTheDocument();
    expect(triggerOf(7)).toHaveFocus();
  });
});
