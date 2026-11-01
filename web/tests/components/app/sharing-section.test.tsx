import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SharingSection } from "@/components/app/sharing-section";
import type { Actor } from "@/lib/app/actions";
import {
  APP,
  describeCall,
  jsonResponse,
  readExample,
  renderWithActions,
  stubApi,
} from "./form-test-harness";

afterEach(() => {
  vi.restoreAllMocks();
});

const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const VISITOR: Actor = {
  email: "bob@example.com",
  isPlatformAdmin: false,
  permission: "CAN_USE",
  canManage: false,
};

function renderSection(actor: Actor | null) {
  const view = renderWithActions((actions) => (
    <SharingSection
      app={APP}
      actor={actor}
      actions={actions}
      onReload={vi.fn()}
    />
  ));
  const table = screen.getByRole("table", { name: "Who can open this app" });
  const row = (name: RegExp) => within(table).getByRole("row", { name });
  return { ...view, row, user: userEvent.setup() };
}

describe("SharingSection", () => {
  it("lists the owner first, who cannot be removed, then the grants with their level", () => {
    const { row } = renderSection(MANAGER);

    const ownerRow = row(/Owner/);
    // Row 0 is the header.
    expect(
      within(screen.getByRole("table", { name: "Who can open this app" }))
        .getAllByRole("row")
        .indexOf(ownerRow),
    ).toBe(1);
    expect(ownerRow).toHaveTextContent("alice@example.com");
    expect(
      within(ownerRow).getByRole("button", { name: "Remove" }),
    ).toBeDisabled();
    expect(
      screen.getByText(/The owner always manages the app/),
    ).toBeInTheDocument();
    expect(row(/sales-team/)).toHaveTextContent("Group");
    expect(row(/sales-team/)).toHaveTextContent("Can use");
    expect(row(/carol@example\.com/)).toHaveTextContent("Can manage");
  });

  it("asks before removing, then deletes the grant by its type and name", async () => {
    const calls = stubApi(() => new Response(null, { status: 204 }));
    const { row, user, onChanged } = renderSection(MANAGER);

    await user.click(
      within(row(/sales-team/)).getByRole("button", { name: "Remove" }),
    );
    expect(calls).toHaveLength(0);
    expect(
      screen.getByRole("group", {
        name: "Remove group sales-team (Can use)?",
      }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Yes, remove" }));

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "DELETE",
      path: "/apps/sales/grants/group/sales-team",
      body: undefined,
    });
  });

  it("changes the level of a grant to the other one", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("AppGrant")));
    const { row, user, onChanged } = renderSection(MANAGER);

    await user.click(
      within(row(/sales-team/)).getByRole("button", {
        name: "Make admin sales-team",
      }),
    );

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "PUT",
      path: "/apps/sales/grants",
      body: { type: "group", name: "sales-team", permission: "CAN_MANAGE" },
    });
  });

  it("shows a failed change where the person is looking", async () => {
    stubApi(() =>
      jsonResponse(403, {
        error: { code: "forbidden", message: "not your app" },
      }),
    );
    const { row, user } = renderSection(MANAGER);

    await user.click(
      within(row(/sales-team/)).getByRole("button", {
        name: "Make admin sales-team",
      }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not change the level of sales-team");
    expect(alert).toHaveTextContent("not your app");
  });

  it("leaves a plain visitor every control off, with the reason beside them", () => {
    const { row } = renderSection(VISITOR);

    expect(
      within(row(/sales-team/)).getByRole("button", { name: "Remove" }),
    ).toBeDisabled();
    expect(
      within(row(/sales-team/)).getByRole("button", {
        name: "Make admin sales-team",
      }),
    ).toBeDisabled();
    expect(
      screen.getByText("Sharing: Only an admin of this app can do this."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Share" })).toBeDisabled();
  });
});
