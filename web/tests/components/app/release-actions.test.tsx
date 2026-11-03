import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppDetail } from "@/lib/api";
import type { ReleaseSummary } from "@/lib/api";
import { REASON_NOT_MANAGER } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import { ReleaseRowMenu } from "@/components/app/release-actions";
import type { ReleaseRowMenuProps } from "@/components/app/release-actions";
import type { AppActions } from "@/components/app/use-app-actions";
import { readExample } from "../../support/contract-examples";

const app = AppDetail.parse(readExample("AppDetail"));

function releaseNo(no: number): ReleaseSummary {
  const found = app.releases.find((release) => release.release_no === no);
  if (found === undefined) throw new Error(`the example has no release ${no}`);
  return found;
}

const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const USER_ONLY: Actor = {
  ...MANAGER,
  permission: "CAN_USE",
  canManage: false,
};

const IDLE: AppActions = {
  stateOf: () => "idle",
  run: () => Promise.reject(new Error("not used by this component")),
};

function setup(
  no: number,
  overrides: Partial<
    Pick<ReleaseRowMenuProps, "actor" | "actions" | "isViewingSource">
  > = {},
) {
  const release = releaseNo(no);
  const onSelect = vi.fn();
  const user = userEvent.setup();
  render(
    <ReleaseRowMenu
      app={app}
      release={release}
      actor={MANAGER}
      actions={IDLE}
      isViewingSource={false}
      triggerRef={null}
      onSelect={onSelect}
      {...overrides}
    />,
  );
  const trigger = screen.getByRole("button", {
    name: `Actions for release ${String(no)}`,
  });
  return { release, onSelect, user, trigger };
}

function item(name: RegExp): HTMLElement {
  return within(screen.getByRole("menu")).getByRole("menuitem", { name });
}

describe("ReleaseRowMenu", () => {
  it("opens on a click and lists the actions of the release, Delete last", async () => {
    const { user, trigger } = setup(6);
    await user.click(trigger);

    const menu = screen.getByRole("menu");
    expect(within(menu).getByText("Release 6")).toBeInTheDocument();
    const names = within(menu)
      .getAllByRole("menuitem")
      .map((entry) => entry.firstElementChild?.textContent);
    expect(names).toEqual([
      "Activate",
      "Rename",
      "Tag",
      "Lock",
      "View source",
      "Delete",
    ]);
    expect(item(/^Activate/)).toHaveTextContent(/rolls back/i);
  });

  it("hands the chosen action and its release to the section, and closes", async () => {
    const { release, onSelect, user, trigger } = setup(6);
    await user.click(trigger);
    await user.click(item(/^Delete/));

    expect(onSelect).toHaveBeenCalledExactlyOnceWith("delete", release);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("gives the keyboard back to the trigger after an action that opens no panel", async () => {
    const { release, onSelect, user, trigger } = setup(6);
    trigger.focus();
    await user.keyboard("{Enter}");
    // Activate, Rename, Tag, Lock.
    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}{Enter}");

    expect(onSelect).toHaveBeenCalledExactlyOnceWith("lock", release);
    expect(trigger).toHaveFocus();
  });

  it("keeps an action it refuses reachable on the keyboard, says why, and does nothing when chosen", async () => {
    // Release 7 is the active release.
    const { onSelect, user, trigger } = setup(7);
    trigger.focus();
    await user.keyboard("{Enter}{End}");

    const remove = item(/^Delete/);
    expect(remove).toHaveFocus();
    expect(remove).toHaveAttribute("aria-disabled", "true");
    expect(remove).toHaveTextContent("The active release cannot be deleted.");

    await user.keyboard("{Enter}");
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("offers Unlock instead of Lock on a release locked by hand", async () => {
    const { release, onSelect, user, trigger } = setup(5);
    await user.click(trigger);
    expect(
      within(screen.getByRole("menu")).queryByRole("menuitem", {
        name: /^Lock/,
      }),
    ).not.toBeInTheDocument();
    await user.click(item(/^Unlock/));
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("unlock", release);
  });

  it("refuses every action to someone who cannot manage the app, with the reason on each", async () => {
    const { user, trigger } = setup(6, { actor: USER_ONLY });
    await user.click(trigger);
    for (const entry of within(screen.getByRole("menu")).getAllByRole(
      "menuitem",
    )) {
      expect(entry).toHaveAttribute("aria-disabled", "true");
      expect(entry).toHaveTextContent(REASON_NOT_MANAGER);
    }
  });

  it("names the source item Hide source while the viewer shows this release", async () => {
    const { user, trigger } = setup(6, { isViewingSource: true });
    await user.click(trigger);
    expect(item(/^Hide source/)).toBeInTheDocument();
  });

  it("shows a running call beside the trigger and holds the other actions until it ends", async () => {
    const { user, trigger } = setup(6, {
      actions: {
        ...IDLE,
        stateOf: (key) => (key === "lock:6" ? "busy" : "blocked"),
      },
    });
    expect(screen.getByText("Locking")).toBeInTheDocument();

    await user.click(trigger);
    expect(item(/^Lock/)).toHaveAttribute("aria-disabled", "true");
    expect(item(/^Activate/)).toHaveTextContent(
      "Another change is being saved; wait for it.",
    );
    // Reading the source writes nothing, so a running write does not hold it.
    expect(item(/^View source/)).not.toHaveAttribute("aria-disabled");
  });
});
