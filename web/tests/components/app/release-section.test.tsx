import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as Api from "@/lib/api";
import { ApiError, AppDetail, ReleaseSourceListing } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import type { WriteResult } from "@/lib/app/writes";
import { ReleaseSection } from "@/components/app/release-section";
import type { AppActions } from "@/components/app/use-app-actions";
import { readExample } from "../../support/contract-examples";

const { deleteRelease, listReleaseSource } = vi.hoisted(() => ({
  deleteRelease: vi.fn(),
  listReleaseSource: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof Api>()),
  deleteRelease,
  listReleaseSource,
}));

const app = AppDetail.parse(readExample("AppDetail"));
const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};

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
    .find((cell) => cell.textContent.startsWith(`#${no}`));
  const row = header?.closest("tr");
  if (!row) throw new Error(`no row for release ${no}`);
  return row;
}

beforeEach(() => {
  deleteRelease.mockReset();
  listReleaseSource.mockReset();
});

describe("ReleaseSection", () => {
  it("lists the releases newest first, each row with its own actions", () => {
    setup();
    const table = screen.getByRole("table", {
      name: "Releases of this app, newest first",
    });
    const numbers = within(table)
      .getAllByRole("rowheader")
      .map((cell) => /^#(\d+)/.exec(cell.textContent)?.[1]);
    expect(numbers).toEqual(["8", "7", "6", "5"]);
    expect(
      within(rowOf(6)).getByRole("button", { name: "Activate release 6" }),
    ).toBeEnabled();
  });

  it("deletes only the release of the row, and only after the question is confirmed", async () => {
    deleteRelease.mockResolvedValue(undefined);
    const { user } = setup();
    await user.click(within(rowOf(6)).getByRole("button", { name: "Delete" }));
    expect(deleteRelease).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "Yes, delete release 6" }),
    );
    expect(deleteRelease).toHaveBeenCalledExactlyOnceWith("sales", 6);
  });

  it("shows why a write failed under the table, with a reload after a 409", async () => {
    deleteRelease.mockRejectedValue(
      new ApiError(409, "conflict", "release 6 is the target now"),
    );
    const { onReload, user } = setup();
    await user.click(within(rowOf(6)).getByRole("button", { name: "Delete" }));
    await user.click(
      screen.getByRole("button", { name: "Yes, delete release 6" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not delete release 6",
    );
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("opens the edit form of the pressed row and closes it on a second press", async () => {
    const { user } = setup();
    await user.click(
      within(rowOf(6)).getByRole("button", { name: "Edit release 6" }),
    );
    expect(
      screen.getByRole("form", { name: "Edit release 6" }),
    ).toBeInTheDocument();
    expect(
      within(rowOf(6)).getByRole("button", { name: "Edit release 6" }),
    ).toHaveAttribute("aria-expanded", "true");

    await user.click(
      within(rowOf(6)).getByRole("button", { name: "Edit release 6" }),
    );
    expect(
      screen.queryByRole("form", { name: "Edit release 6" }),
    ).not.toBeInTheDocument();
  });

  it("reads the source listing only when View source is pressed, and closes it on Close", async () => {
    listReleaseSource.mockResolvedValue(
      ReleaseSourceListing.parse(readExample("ReleaseSourceListing")),
    );
    const { user } = setup();
    expect(listReleaseSource).not.toHaveBeenCalled();

    await user.click(
      within(rowOf(7)).getByRole("button", { name: "View source release 7" }),
    );
    expect(
      await screen.findByRole("region", {
        name: "Source of release 7 of sales",
      }),
    ).toBeInTheDocument();
    expect(listReleaseSource).toHaveBeenCalledExactlyOnceWith("sales", 7);

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen.queryByRole("region", { name: "Source of release 7 of sales" }),
    ).not.toBeInTheDocument();
  });
});
