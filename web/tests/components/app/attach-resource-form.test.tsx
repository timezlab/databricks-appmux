import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AttachResourceForm } from "@/components/app/attach-resource-form";
import { attachDraftFromOffer, emptyAttachDraft } from "@/lib/app/resources";
import type { AttachDraft } from "@/lib/app/resources";
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

function renderForm(initialDraft: AttachDraft = emptyAttachDraft()) {
  const onClose = vi.fn();
  const view = renderWithActions((actions) => (
    <AttachResourceForm
      app={APP}
      initialDraft={initialDraft}
      actions={actions}
      onClose={onClose}
      onReload={vi.fn()}
    />
  ));
  return { ...view, onClose, user: userEvent.setup() };
}

describe("AttachResourceForm", () => {
  it("opens with the keyboard on the first field", () => {
    renderForm();
    expect(screen.getByRole("textbox", { name: /Key/ })).toHaveFocus();
  });

  it("names every missing field, announces the problem and puts the keyboard on the first one", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("ResourceView")));
    const { user } = renderForm();

    await user.click(screen.getByRole("button", { name: "Attach" }));

    const key = screen.getByRole("textbox", { name: /Key/ });
    const target = screen.getByRole("textbox", { name: /Warehouse id/ });
    const accessAs = screen.getByRole("combobox", { name: /Access as/ });
    expect(key).toBeInvalid();
    expect(key).toHaveAccessibleDescription(/Enter a key/);
    expect(target).toBeInvalid();
    expect(accessAs).toBeInvalid();
    expect(accessAs).toHaveAccessibleDescription(
      /Choose the identity the app reaches it as\./,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The resource was not attached",
    );
    expect(key).toHaveFocus();
    expect(calls).toHaveLength(0);
  });

  it("refuses a key that is already attached", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("ResourceView")));
    const { user } = renderForm();

    await user.type(
      screen.getByRole("textbox", { name: /Key/ }),
      "sql-warehouse",
    );
    await user.click(screen.getByRole("button", { name: "Attach" }));

    expect(
      screen.getByRole("textbox", { name: /Key/ }),
    ).toHaveAccessibleDescription(/already attached/);
    expect(calls).toHaveLength(0);
  });

  it("sends the key in the path and the rest as the body, then closes", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("ResourceView")));
    const { user, onClose, onChanged } = renderForm();

    await user.type(
      screen.getByRole("textbox", { name: /Key/ }),
      "warehouse-2",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Warehouse id/ }),
      " 0123abcd ",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: /Access as/ }),
      "As the visitor (OBO)",
    );
    await user.click(screen.getByRole("button", { name: "Attach" }));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(describeCall(calls[0])).toEqual({
      method: "PUT",
      path: "/apps/sales/resources/warehouse-2",
      body: {
        type: "sql_warehouse",
        target: "0123abcd",
        permission: "CAN_USE",
        access_as: "obo",
      },
    });
  });

  it("keeps an identity the app cannot use out of reach and says why", () => {
    renderForm();
    const shared = screen.getByRole("option", {
      name: /As the appmux service principal/,
    });
    expect(shared).toBeDisabled();
    expect(shared).toHaveTextContent("(not available)");
    expect(
      screen.getByText(/switched shared service principal delegation off/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "As the visitor (OBO)" }),
    ).toBeEnabled();
  });

  describe("a secret", () => {
    async function chooseSecret(user: ReturnType<typeof userEvent.setup>) {
      await user.selectOptions(
        screen.getByRole("combobox", { name: /Type/ }),
        "Secret",
      );
    }

    it("is a scope/key reference: the field says never to type the value, and there is no identity to choose", async () => {
      const { user } = renderForm();
      await chooseSecret(user);

      const reference = screen.getByRole("textbox", {
        name: /Secret reference/,
      });
      expect(reference).toHaveAccessibleDescription(
        /Never type the secret value\./,
      );
      expect(screen.queryByRole("combobox", { name: /Access as/ })).toBeNull();
      expect(
        screen.getByText(/appmux reads a secret itself when the app starts/),
      ).toBeInTheDocument();
    });

    it("refuses something that is not a reference, and never sends it", async () => {
      const calls = stubApi(() =>
        jsonResponse(200, readExample("ResourceView")),
      );
      const { user } = renderForm();
      await chooseSecret(user);

      await user.type(screen.getByRole("textbox", { name: /Key/ }), "api-key");
      const reference = screen.getByRole("textbox", {
        name: /Secret reference/,
      });
      await user.type(reference, "sk-not-a-reference-just-a-value");
      await user.click(screen.getByRole("button", { name: "Attach" }));

      expect(reference).toBeInvalid();
      expect(reference).toHaveAccessibleDescription(/Use scope\/key/);
      expect(calls).toHaveLength(0);
    });

    it("sends the reference with the one level READ and no identity", async () => {
      const calls = stubApi(() =>
        jsonResponse(200, readExample("ResourceView")),
      );
      const { user, onClose } = renderForm();
      await chooseSecret(user);

      await user.type(screen.getByRole("textbox", { name: /Key/ }), "api-key");
      await user.type(
        screen.getByRole("textbox", { name: /Secret reference/ }),
        "sales-secrets/api-key",
      );
      await user.click(screen.getByRole("button", { name: "Attach" }));

      await waitFor(() => {
        expect(onClose).toHaveBeenCalled();
      });
      expect(describeCall(calls[0]).body).toEqual({
        type: "secret",
        target: "sales-secrets/api-key",
        permission: "READ",
      });
    });
  });

  it("starts from an offer of the release's databricks.yml and says so to the server", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("ResourceView")));
    const { user, onClose } = renderForm(
      attachDraftFromOffer({
        key: "forecast",
        type: "serving_endpoint",
        target: "sales-forecast",
        permission: "CAN_QUERY",
      }),
    );
    expect(screen.getByRole("textbox", { name: /Key/ })).toHaveValue(
      "forecast",
    );
    expect(screen.getByText(/Filled in from the release/)).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: /Access as/ }),
      "As the visitor (OBO)",
    );
    await user.click(screen.getByRole("button", { name: "Attach" }));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalled();
    });
    expect(describeCall(calls[0]).body).toEqual({
      type: "serving_endpoint",
      target: "sales-forecast",
      permission: "CAN_QUERY",
      access_as: "obo",
      from_bundle: true,
    });
  });

  it("shows the server's refusal and keeps the form open", async () => {
    stubApi(() =>
      jsonResponse(403, {
        error: { code: "forbidden", message: "your token cannot reach it" },
      }),
    );
    const { user, onClose } = renderForm();
    await user.type(
      screen.getByRole("textbox", { name: /Key/ }),
      "warehouse-2",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Warehouse id/ }),
      "0123abcd",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: /Access as/ }),
      "As the visitor (OBO)",
    );
    await user.click(screen.getByRole("button", { name: "Attach" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "your token cannot reach it",
    );
    expect(onClose).not.toHaveBeenCalled();
  });
});
