import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as Api from "@/lib/api";
import { ApiError, AppDetail } from "@/lib/api";
import type { ReleaseSummary } from "@/lib/api";
import type { WriteResult, WriteState } from "@/lib/app/writes";
import { ReleaseEditForm } from "@/components/app/release-edit-form";
import type { AppActions } from "@/components/app/use-app-actions";
import { readExample } from "../../support/contract-examples";

const { updateRelease } = vi.hoisted(() => ({ updateRelease: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof Api>()),
  updateRelease,
}));

const app = AppDetail.parse(readExample("AppDetail"));
// Release 7: named "fix pagination", tagged "stable".
function findRelease(releaseNo: number): ReleaseSummary {
  const found = app.releases.find((item) => item.release_no === releaseNo);
  if (found === undefined) {
    throw new Error(`the example has no release ${releaseNo}`);
  }
  return found;
}
const release = findRelease(7);

/** Runs the call at once and hands back the outcome, as the page's write controller does. */
function actionsIn(state: WriteState): AppActions {
  return {
    stateOf: () => state,
    run: async <T,>(key: string, call: () => Promise<T>) => {
      try {
        return { ok: true, value: await call() } satisfies WriteResult<T>;
      } catch (error) {
        return { ok: false, error } satisfies WriteResult<T>;
      }
    },
  };
}

function setup(state: WriteState = "idle") {
  const onClose = vi.fn();
  const onReload = vi.fn();
  const user = userEvent.setup();
  render(
    <ReleaseEditForm
      app={app}
      release={release}
      actions={actionsIn(state)}
      onClose={onClose}
      onReload={onReload}
    />,
  );
  return { onClose, onReload, user };
}

beforeEach(() => {
  updateRelease.mockReset();
});

describe("ReleaseEditForm", () => {
  it("opens with the release's name and tags, the keyboard on the name, and Save off until something changes", () => {
    setup();
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue(
      "fix pagination",
    );
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Tags" })).toHaveValue("stable");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("sends both values, trimmed and lower-cased, then closes", async () => {
    updateRelease.mockResolvedValue({});
    const { onClose, user } = setup();
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.clear(name);
    await user.type(name, "  candidate ");
    const tags = screen.getByRole("textbox", { name: "Tags" });
    await user.clear(tags);
    await user.type(tags, "Stable, Beta stable");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(updateRelease).toHaveBeenCalledExactlyOnceWith("sales", 7, {
      name: "candidate",
      tags: ["stable", "beta"],
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("announces a tag that is too long, sends nothing and moves focus to the field", async () => {
    const { onClose, user } = setup();
    const tags = screen.getByRole("textbox", { name: "Tags" });
    await user.clear(tags);
    await user.type(tags, "x".repeat(33));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(updateRelease).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The release was not saved",
    );
    expect(tags).toBeInvalid();
    expect(tags).toHaveAccessibleDescription(
      /Each tag has at most 32 characters\./,
    );
    expect(tags).toHaveFocus();
  });

  it("clears a field's error as soon as that field is edited", async () => {
    const { user } = setup();
    const tags = screen.getByRole("textbox", { name: "Tags" });
    await user.clear(tags);
    await user.type(tags, "x".repeat(33));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(tags).toBeInvalid();

    await user.type(tags, "{Backspace}");
    expect(tags).toBeValid();
  });

  it("keeps the form open and offers a reload when the server answers 409", async () => {
    updateRelease.mockRejectedValue(
      new ApiError(409, "conflict", "release 7 was deleted"),
    );
    const { onClose, onReload, user } = setup();
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.type(name, "!");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The release was not saved",
    );
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("does not send a second time while a save is running", async () => {
    const { user } = setup("busy");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "!{Enter}");

    const save = screen.getByRole("button", { name: "Saving" });
    expect(save).toHaveAttribute("aria-busy", "true");
    await user.click(save);
    expect(updateRelease).not.toHaveBeenCalled();
  });

  it("turns Save off while another change is running", () => {
    setup("blocked");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});
