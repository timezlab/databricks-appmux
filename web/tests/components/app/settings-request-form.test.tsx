import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsRequestForm } from "@/components/app/settings-request-form";
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

function renderForm() {
  const onClose = vi.fn();
  const view = renderWithActions((actions) => (
    <SettingsRequestForm
      app={APP}
      actions={actions}
      onClose={onClose}
      onReload={vi.fn()}
    />
  ));
  return { ...view, onClose, user: userEvent.setup() };
}

describe("SettingsRequestForm", () => {
  it("opens on the slug, with every field filled in from the app", () => {
    renderForm();
    expect(screen.getByRole("textbox", { name: "Slug" })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Slug" })).toHaveValue("sales");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue(
      "Sales dashboard",
    );
    expect(
      screen.getByRole("textbox", { name: "Memory budget (MB)" }),
    ).toHaveValue("512");
  });

  it("needs a reason: the field is marked, announced and focused, and nothing is sent", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("RequestRow")));
    const { user } = renderForm();

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

  it("needs a change to ask for: a reason alone is not a request", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("RequestRow")));
    const { user } = renderForm();

    await user.type(
      screen.getByRole("textbox", { name: /Reason/ }),
      "Just ask",
    );
    await user.click(screen.getByRole("button", { name: "Send request" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Change at least one field before asking.",
    );
    expect(calls).toHaveLength(0);
  });

  it("sends only the fields that differ from the app's, with the reason", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("RequestRow")));
    const { user, onClose, onChanged } = renderForm();

    const name = screen.getByRole("textbox", { name: "Name" });
    await user.clear(name);
    await user.type(name, "Revenue dashboard");
    const memory = screen.getByRole("textbox", { name: "Memory budget (MB)" });
    await user.clear(memory);
    await user.type(memory, "768");
    await user.type(
      screen.getByRole("textbox", { name: /Reason/ }),
      "More data to chart",
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
        kind: "settings",
        app: "sales",
        payload: { display_name: "Revenue dashboard", memory_budget_mb: 768 },
        reason: "More data to chart",
      },
    });
  });

  it("refuses a memory budget that is not a whole number and puts the keyboard on it", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("RequestRow")));
    const { user } = renderForm();

    const memory = screen.getByRole("textbox", { name: "Memory budget (MB)" });
    await user.clear(memory);
    await user.type(memory, "lots");
    await user.type(screen.getByRole("textbox", { name: /Reason/ }), "Why not");
    await user.click(screen.getByRole("button", { name: "Send request" }));

    expect(memory).toBeInvalid();
    expect(memory).toHaveAccessibleDescription(/Use a whole number of MB/);
    expect(memory).toHaveFocus();
    expect(calls).toHaveLength(0);
  });

  it("keeps the form open and shows the server's refusal", async () => {
    stubApi(() =>
      jsonResponse(409, {
        error: {
          code: "slug_taken",
          message: "slug 'revenue' is already taken",
        },
      }),
    );
    const { user, onClose } = renderForm();
    const slug = screen.getByRole("textbox", { name: "Slug" });
    await user.clear(slug);
    await user.type(slug, "revenue");
    await user.type(screen.getByRole("textbox", { name: /Reason/ }), "Rename");
    await user.click(screen.getByRole("button", { name: "Send request" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("slug 'revenue' is already taken");
    expect(onClose).not.toHaveBeenCalled();
    expect(slug).toHaveValue("revenue");
  });
});
