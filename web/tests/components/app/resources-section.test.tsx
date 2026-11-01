import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResourcesSection } from "@/components/app/resources-section";
import type { Actor } from "@/lib/app/actions";
import { emptyAttachDraft } from "@/lib/app/resources";
import type { AttachDraft } from "@/lib/app/resources";
import {
  APP,
  appWith,
  describeCall,
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
const VISITOR: Actor = { ...MANAGER, permission: "CAN_USE", canManage: false };

function renderSection({
  actor = MANAGER,
  draft = null,
}: { actor?: Actor; draft?: AttachDraft | null } = {}) {
  const onOpenForm = vi.fn();
  const onCloseForm = vi.fn();
  const view = renderWithActions((actions) => (
    <ResourcesSection
      app={APP}
      actor={actor}
      actions={actions}
      onReload={vi.fn()}
      draft={draft}
      draftId={1}
      onOpenForm={onOpenForm}
      onCloseForm={onCloseForm}
    />
  ));
  const table = screen.getByRole("table", {
    name: "Resources attached to this app",
  });
  return {
    ...view,
    onOpenForm,
    onCloseForm,
    row: (name: RegExp) => within(table).getByRole("row", { name }),
    user: userEvent.setup(),
  };
}

describe("ResourcesSection", () => {
  it("shows a secret as its scope/key reference with no identity, never as a value", () => {
    const { row } = renderSection();

    const secret = row(/openai-api-key/);
    expect(secret).toHaveTextContent("sales-secrets/openai-api-key");
    expect(secret).toHaveTextContent("Read by appmux when the app starts");
    expect(row(/sql-warehouse/)).toHaveTextContent("As the visitor (OBO)");
  });

  it("asks before detaching, names what still reads the resource, then deletes it by key", async () => {
    const calls = stubApi(() => new Response(null, { status: 204 }));
    const { row, user, onChanged } = renderSection();

    await user.click(
      within(row(/sql-warehouse/)).getByRole("button", { name: "Detach" }),
    );
    expect(calls).toHaveLength(0);
    expect(
      screen.getByRole("group", {
        name: /Detach sql-warehouse\? Still read by the env entry WAREHOUSE\./,
      }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Yes, detach sql-warehouse" }),
    );

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "DELETE",
      path: "/apps/sales/resources/sql-warehouse",
      body: undefined,
    });
  });

  it("opens and closes the attach form with one button that says which it will do", async () => {
    const closed = renderSection();
    const openButton = screen.getByRole("button", {
      name: "Attach a resource",
    });
    expect(openButton).toHaveAttribute("aria-expanded", "false");
    await closed.user.click(openButton);
    expect(closed.onOpenForm).toHaveBeenCalledTimes(1);
    closed.unmount();

    const open = renderSection({ draft: emptyAttachDraft() });
    expect(
      screen.getByRole("form", { name: "Attach a resource" }),
    ).toBeInTheDocument();
    const closeButton = screen.getByRole("button", { name: "Close the form" });
    expect(closeButton).toHaveAttribute("aria-expanded", "true");
    await open.user.click(closeButton);
    expect(open.onCloseForm).toHaveBeenCalledTimes(1);
  });

  it("turns Attach and Detach off for a visitor and says why", () => {
    const { row } = renderSection({ actor: VISITOR });

    expect(
      screen.getByRole("button", { name: "Attach a resource" }),
    ).toBeDisabled();
    expect(
      within(row(/sql-warehouse/)).getByRole("button", { name: "Detach" }),
    ).toBeDisabled();
    expect(
      screen.getByText(
        "Attach and Detach: Only an admin of this app can do this.",
      ),
    ).toBeInTheDocument();
  });

  it("says what to do when nothing is attached", () => {
    const view = renderWithActions((actions) => (
      <ResourcesSection
        app={appWith({ resources: [] })}
        actor={MANAGER}
        actions={actions}
        onReload={vi.fn()}
        draft={null}
        draftId={0}
        onOpenForm={vi.fn()}
        onCloseForm={vi.fn()}
      />
    ));
    expect(view.container).toHaveTextContent("No resources attached");
    expect(screen.queryByRole("table")).toBeNull();
  });
});
