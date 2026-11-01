import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DelegationRequestForm } from "@/components/app/delegation-request-form";
import type { AppDetail } from "@/lib/api";
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

// Nothing delegated yet and the platform allows both: every box can be ticked.
const ASKABLE: AppDetail = appWith({
  delegation: {
    obo_delegated: false,
    shared_sp_delegated: false,
    shared_sp_delegation_allowed: true,
  },
});

function renderForm(app: AppDetail = ASKABLE) {
  const onClose = vi.fn();
  const view = renderWithActions((actions) => (
    <DelegationRequestForm
      app={app}
      actions={actions}
      onClose={onClose}
      onReload={vi.fn()}
    />
  ));
  return { ...view, onClose, user: userEvent.setup() };
}

const OBO = /Act on behalf of the people who open the app/;
const SHARED_SP = /Act as the appmux service principal/;

describe("DelegationRequestForm", () => {
  it("needs a reason: the field is marked, announced and focused, and nothing is sent", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("RequestRow")));
    const { user } = renderForm();

    await user.click(screen.getByRole("checkbox", { name: OBO }));
    await user.click(screen.getByRole("button", { name: "Send request" }));

    const reason = screen.getByRole("textbox", { name: /Reason/ });
    expect(reason).toBeInvalid();
    expect(reason).toHaveAccessibleDescription(
      /Enter a reason; a platform admin reads it\./,
    );
    expect(reason).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The request was not sent",
    );
    expect(calls).toHaveLength(0);
  });

  it("needs at least one delegation ticked", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("RequestRow")));
    const { user } = renderForm();

    await user.type(
      screen.getByRole("textbox", { name: /Reason/ }),
      "We need it",
    );
    await user.click(screen.getByRole("button", { name: "Send request" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Choose at least one delegation to ask for.",
    );
    expect(calls).toHaveLength(0);
  });

  it("sends the ticked delegations and the trimmed reason, then closes", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("RequestRow")));
    const { user, onClose, onChanged } = renderForm();

    await user.click(screen.getByRole("checkbox", { name: OBO }));
    await user.click(screen.getByRole("checkbox", { name: SHARED_SP }));
    await user.type(
      screen.getByRole("textbox", { name: /Reason/ }),
      "  Show each reader their own files  ",
    );
    await user.click(screen.getByRole("button", { name: "Send request" }));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(describeCall(calls[0])).toEqual({
      method: "POST",
      path: "/requests",
      body: {
        kind: "delegation",
        app: "sales",
        payload: { obo: true, shared_sp: true },
        reason: "Show each reader their own files",
      },
    });
  });

  it("leaves a delegation that is granted or switched off unticked, says why, and opens on the reason", () => {
    // The contract's app: OBO is delegated already, the platform forbids the shared service principal.
    renderForm(APP);

    const obo = screen.getByRole("checkbox", { name: OBO });
    const sharedSp = screen.getByRole("checkbox", { name: SHARED_SP });
    expect(obo).toBeDisabled();
    expect(obo).toHaveAccessibleDescription(/Already delegated to this app\./);
    expect(sharedSp).toBeDisabled();
    expect(sharedSp).toHaveAccessibleDescription(
      /A platform admin has switched this delegation off/,
    );
    expect(screen.getByRole("textbox", { name: /Reason/ })).toHaveFocus();
  });

  it("counts the reason's characters in its hint", async () => {
    const { user } = renderForm();
    await user.type(screen.getByRole("textbox", { name: /Reason/ }), "abcde");
    expect(
      screen.getByRole("textbox", { name: /Reason/ }),
    ).toHaveAccessibleDescription(/5 of \d+ characters/);
  });
});
