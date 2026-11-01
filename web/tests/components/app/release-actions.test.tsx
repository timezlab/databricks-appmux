import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppDetail } from "@/lib/api";
import type { ReleaseSummary } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { ReleaseActions } from "@/components/app/release-actions";
import type { ReleaseActionsProps } from "@/components/app/release-actions";
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

function setup(
  no: number,
  overrides: Partial<Pick<ReleaseActionsProps, "actor" | "isEditing">> = {},
) {
  const release = releaseNo(no);
  const onWrite = vi.fn();
  const onToggleEdit = vi.fn();
  const onToggleSource = vi.fn();
  const user = userEvent.setup();
  render(
    <ReleaseActions
      app={app}
      release={release}
      actor={MANAGER}
      actions={{
        stateOf: () => "idle",
        run: () => Promise.reject(new Error("not used by this component")),
      }}
      isEditing={false}
      isViewingSource={false}
      onWrite={onWrite}
      onToggleEdit={onToggleEdit}
      onToggleSource={onToggleSource}
      {...overrides}
    />,
  );
  return { release, onWrite, onToggleEdit, onToggleSource, user };
}

describe("ReleaseActions", () => {
  it("does not delete on the first press: it asks, and only the confirm button deletes", async () => {
    // Release 6: superseded, not the target, not locked.
    const { release, onWrite, user } = setup(6);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(onWrite).not.toHaveBeenCalled();

    const question = screen.getByRole("group", {
      name: /^Delete release 6\?/,
    });
    await user.click(
      within(question).getByRole("button", { name: "Yes, delete release 6" }),
    );
    expect(onWrite).toHaveBeenCalledExactlyOnceWith("delete", release);
  });

  it("cancelling the question deletes nothing", async () => {
    const { onWrite, user } = setup(6);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onWrite).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeEnabled();
  });

  it.each([
    [5, "Locked; unlock this release first."],
    [7, "The active release cannot be deleted."],
    [8, "This release is the target; activate another release first."],
  ])("turns Delete off for release %i and says why in words", (no, reason) => {
    setup(no);
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(screen.getByText(`Delete: ${reason}`)).toBeInTheDocument();
  });

  it("names every button by its release and offers Unlock, not Lock, on a release locked by hand", async () => {
    const { release, onWrite, user } = setup(5);
    expect(
      screen.queryByRole("button", { name: "Lock release 5" }),
    ).not.toBeInTheDocument();
    for (const name of [
      "Activate release 5",
      "Edit release 5",
      "View source release 5",
    ]) {
      expect(screen.getByRole("button", { name })).toBeEnabled();
    }
    await user.click(screen.getByRole("button", { name: "Unlock release 5" }));
    expect(onWrite).toHaveBeenCalledExactlyOnceWith("unlock", release);
  });

  it("opens the edit and source panels through callbacks and reports which one is open", async () => {
    const { release, onToggleEdit, onToggleSource, user } = setup(6, {
      isEditing: true,
    });
    const edit = screen.getByRole("button", { name: "Edit release 6" });
    expect(edit).toHaveAttribute("aria-expanded", "true");
    const source = screen.getByRole("button", {
      name: "View source release 6",
    });
    expect(source).toHaveAttribute("aria-expanded", "false");

    await user.click(edit);
    await user.click(source);
    expect(onToggleEdit).toHaveBeenCalledExactlyOnceWith(release);
    expect(onToggleSource).toHaveBeenCalledExactlyOnceWith(release);
  });

  it("turns every button off for someone who cannot manage the app, with one shared reason", () => {
    setup(6, { actor: USER_ONLY });
    for (const name of [
      "Activate release 6",
      "Lock release 6",
      "Edit release 6",
      "View source release 6",
      "Delete",
    ]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(
      screen.getByText(
        "Activate, Lock, Delete, Edit and View source: Only an admin of this app can do this.",
      ),
    ).toBeInTheDocument();
  });

  it("turns every button off while it is not yet known who the person is", () => {
    setup(6, { actor: null });
    expect(
      screen.getByRole("button", { name: "Activate release 6" }),
    ).toBeDisabled();
    expect(
      screen.getByText(/Checking what you may do here/),
    ).toBeInTheDocument();
  });
});
