import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  IdentitySection,
  PermissionsSection,
} from "@/components/app/authorization-section";
import type { AppDetail, RequestRow } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import {
  APP,
  appWith,
  describeCall,
  jsonResponse,
  readExample,
  renderWithActions,
  stubApi,
} from "./form-test-harness";

afterEach(() => {
  vi.restoreAllMocks();
});

const OWNER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const CAROL: Actor = {
  email: "carol@example.com",
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

function renderPermissions(actor: Actor | null) {
  const view = renderWithActions((actions) => (
    <PermissionsSection
      app={APP}
      actor={actor}
      actions={actions}
      onReload={vi.fn()}
    />
  ));
  const table = () =>
    screen.getByRole("table", { name: "Who can open or manage this app" });
  const row = (name: RegExp) => within(table()).getByRole("row", { name });
  return { ...view, table, row, user: userEvent.setup() };
}

describe("PermissionsSection", () => {
  it("lists the owner first, who cannot be removed, then each grant with its type and level", () => {
    const { table, row } = renderPermissions(OWNER);

    // Other rows name alice too, as the one who granted them.
    const ownerRow = row(/^alice@example\.com/);
    // Row 0 is the header.
    expect(within(table()).getAllByRole("row").indexOf(ownerRow)).toBe(1);
    expect(ownerRow).toHaveTextContent("Owner");
    expect(ownerRow).toHaveTextContent("Always, as the owner");
    expect(ownerRow).toHaveTextContent(/The owner always manages the app/);
    expect(
      within(ownerRow).queryByRole("button", { name: "Remove" }),
    ).not.toBeInTheDocument();

    expect(row(/sales-team/)).toHaveTextContent("Group");
    expect(
      within(row(/sales-team/)).getByRole("combobox", {
        name: "Level of sales-team",
      }),
    ).toHaveTextContent("Can use");
    expect(
      within(row(/carol@example\.com/)).getByRole("combobox", {
        name: "Level of carol@example.com",
      }),
    ).toHaveTextContent("Can manage");
  });

  it("changes a level in place: the select shows it while the call runs, then the app reloads", async () => {
    let answer: (response: Response) => void = () => undefined;
    const calls = stubApi(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    );
    const { row, user, onChanged } = renderPermissions(OWNER);

    await user.click(
      within(row(/sales-team/)).getByRole("combobox", {
        name: "Level of sales-team",
      }),
    );
    await user.click(screen.getByRole("option", { name: "Can manage" }));

    const level = within(row(/sales-team/)).getByRole("combobox", {
      name: "Level of sales-team",
    });
    expect(level).toHaveTextContent("Can manage");
    expect(level).toBeDisabled();
    expect(within(row(/sales-team/)).getByRole("status")).toHaveTextContent(
      "Saving",
    );
    // Remove waits for the level change instead of reading as "Removing".
    expect(
      within(row(/sales-team/)).getByRole("button", { name: "Remove" }),
    ).toBeDisabled();

    answer(jsonResponse(200, readExample("AppGrant")));
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "PUT",
      path: "/apps/sales/grants",
      body: { type: "group", name: "sales-team", permission: "CAN_MANAGE" },
    });
  });

  it("shows a refused change in words where the person is looking", async () => {
    stubApi(() =>
      jsonResponse(403, {
        error: { code: "forbidden", message: "not your app" },
      }),
    );
    const { row, user } = renderPermissions(OWNER);

    await user.click(
      within(row(/sales-team/)).getByRole("combobox", {
        name: "Level of sales-team",
      }),
    );
    await user.click(screen.getByRole("option", { name: "Can manage" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not change the level of sales-team");
    expect(alert).toHaveTextContent("not your app");
    // The select shows the level the app still has.
    expect(
      within(row(/sales-team/)).getByRole("combobox", {
        name: "Level of sales-team",
      }),
    ).toHaveTextContent("Can use");
  });

  it("asks an app admin before they lower their own access, and Cancel puts focus back on the select", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("AppGrant")));
    const { row, user, onChanged } = renderPermissions(CAROL);
    const level = () =>
      within(row(/carol@example\.com/)).getByRole("combobox", {
        name: "Level of carol@example.com",
      });

    await user.click(level());
    await user.click(screen.getByRole("option", { name: "Can use" }));

    const question = screen.getByRole("group", {
      name: /Lower your own access to Can use\?/,
    });
    expect(
      within(question).getByRole("button", { name: "Cancel" }),
    ).toHaveFocus();
    expect(calls).toHaveLength(0);

    await user.click(within(question).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(level()).toHaveFocus();
    });

    await user.click(level());
    await user.click(screen.getByRole("option", { name: "Can use" }));
    await user.click(
      screen.getByRole("button", { name: "Yes, lower my access" }),
    );
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0]).body).toEqual({
      type: "user",
      name: "carol@example.com",
      permission: "CAN_USE",
    });
  });

  it("asks before removing, then deletes the grant by its type and name", async () => {
    const calls = stubApi(() => new Response(null, { status: 204 }));
    const { row, user, onChanged } = renderPermissions(OWNER);

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

  it("shows someone who cannot manage the app the list only, and says why", () => {
    const { row } = renderPermissions(VISITOR);

    expect(row(/sales-team/)).toHaveTextContent("Can use");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Remove" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("form", { name: "Add a permission" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Read only: Only an admin of this app can do this."),
    ).toBeInTheDocument();
  });
});

function delegationOpen(): AppDetail {
  return appWith({
    delegation: {
      obo_delegated: false,
      shared_sp_delegated: false,
      shared_sp_delegation_allowed: true,
    },
  });
}

function pendingDelegation(): RequestRow {
  const docs = (
    readExample("RequestList") as { items: RequestRow[] }
  ).items.find((request) => request.kind === "delegation");
  if (docs === undefined) throw new Error("no delegation request example");
  return { ...docs, app_slug: "sales" };
}

function renderIdentity(
  app: AppDetail,
  actor: Actor | null,
  requests: readonly RequestRow[] | null = [],
) {
  const view = renderWithActions((actions) => (
    <IdentitySection
      app={app}
      actor={actor}
      actions={actions}
      requests={requests}
      onReload={vi.fn()}
    />
  ));
  return { ...view, user: userEvent.setup() };
}

describe("IdentitySection", () => {
  it("says which identities are in effect for the example app, and that nothing is left to ask for", () => {
    renderIdentity(APP, OWNER);

    const items = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("As the visitor (OBO)");
    expect(items[0]).toHaveTextContent("Delegated");
    expect(items[1]).toHaveTextContent("Switched off by the platform");
    expect(items[2]).toHaveTextContent("Configured");
    expect(
      screen.getByRole("button", { name: "Request a delegation" }),
    ).toBeDisabled();
    expect(screen.getByText(/Nothing left to ask for/)).toBeInTheDocument();
  });

  it("opens the delegation request in place and gives focus back to the button when it closes", async () => {
    const { user } = renderIdentity(delegationOpen(), OWNER);
    const opener = screen.getByRole("button", { name: "Request a delegation" });

    await user.click(opener);
    const form = screen.getByRole("form", { name: "Request a delegation" });
    expect(opener).toHaveAttribute("aria-expanded", "true");
    expect(within(form).getByRole("checkbox", { name: /OBO/ })).toHaveFocus();

    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("form", { name: "Request a delegation" }),
    ).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("points to a pending request instead of letting a second one be sent", () => {
    renderIdentity(delegationOpen(), OWNER, [pendingDelegation()]);

    expect(
      screen.getByText(/A delegation request is waiting for a platform admin/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "See it under Requests" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Request a delegation" }),
    ).toBeDisabled();
  });

  it("lets only an admin of the app ask, and says so", () => {
    renderIdentity(delegationOpen(), VISITOR);

    expect(
      screen.getByRole("button", { name: "Request a delegation" }),
    ).toBeDisabled();
    expect(
      screen.getByText(/Only an admin of this app can do this/),
    ).toBeInTheDocument();
  });
});
