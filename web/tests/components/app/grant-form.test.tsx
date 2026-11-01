import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GrantForm } from "@/components/app/grant-form";
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

function renderForm(isDisabled = false) {
  const onReload = vi.fn();
  const view = renderWithActions((actions) => (
    <GrantForm
      app={APP}
      actions={actions}
      isDisabled={isDisabled}
      onReload={onReload}
    />
  ));
  return { ...view, onReload, user: userEvent.setup() };
}

describe("GrantForm", () => {
  it("refuses an address without @: an announced error on the field, focus on it, nothing sent", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("AppGrant")));
    const { user } = renderForm();
    const address = screen.getByRole("textbox", { name: /Email address/ });

    await user.type(address, "dana");
    await user.click(screen.getByRole("button", { name: "Share" }));

    expect(address).toBeInvalid();
    expect(address).toHaveAccessibleDescription(
      /Enter an email address, such as user@example\.com\./,
    );
    expect(address).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The access was not changed",
    );
    expect(calls).toHaveLength(0);
  });

  it("sends the address lower-cased with the chosen level, then clears the form and reloads the app", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("AppGrant")));
    const { user, onChanged, onNotice } = renderForm();
    const address = screen.getByRole("textbox", { name: /Email address/ });

    await user.type(address, "  Dana@Example.com ");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Level" }),
      "Can manage",
    );
    await user.click(screen.getByRole("button", { name: "Share" }));

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "PUT",
      path: "/apps/sales/grants",
      body: {
        type: "user",
        name: "dana@example.com",
        permission: "CAN_MANAGE",
      },
    });
    expect(onNotice).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "success" }),
    );
    expect(address).toHaveValue("");
  });

  it("asks for a group by name and sends type group", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("AppGrant")));
    const { user, onChanged } = renderForm();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Who" }),
      "A group",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Group name/ }),
      "sales-team",
    );
    await user.click(screen.getByRole("button", { name: "Share" }));

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalled();
    });
    expect(describeCall(calls[0]).body).toEqual({
      type: "group",
      name: "sales-team",
      permission: "CAN_USE",
    });
  });

  it("gives everyone only Can use: no level to choose, and the schema's body is sent", async () => {
    const calls = stubApi(() => jsonResponse(200, readExample("AppGrant")));
    const { user, onChanged } = renderForm();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Who" }),
      "Everyone",
    );

    expect(screen.queryByRole("combobox", { name: "Level" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(
      screen.getByText(
        /Everyone who signs in to the workspace can open the app/,
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Share" }));
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalled();
    });
    expect(describeCall(calls[0]).body).toEqual({
      type: "everyone",
      name: "*",
      permission: "CAN_USE",
    });
  });

  it("is busy while the call runs: the button says so and a second press sends nothing", async () => {
    let answer: (response: Response) => void = () => undefined;
    const calls = stubApi(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    );
    const { user, onChanged } = renderForm();
    await user.type(
      screen.getByRole("textbox", { name: /Email address/ }),
      "dana@example.com",
    );

    await user.click(screen.getByRole("button", { name: "Share" }));
    const busy = await screen.findByRole("button", { name: "Sharing" });
    expect(busy).toHaveAttribute("aria-busy", "true");
    await user.click(busy);
    expect(calls).toHaveLength(1);

    answer(jsonResponse(200, readExample("AppGrant")));
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByRole("button", { name: "Share" })).not.toHaveAttribute(
      "aria-busy",
    );
  });

  it("is off when the person may not manage: every control is disabled", () => {
    renderForm(true);
    expect(screen.getByRole("combobox", { name: "Who" })).toBeDisabled();
    expect(
      screen.getByRole("textbox", { name: /Email address/ }),
    ).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Level" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Share" })).toBeDisabled();
  });

  it("shows the server's refusal where the person is looking and keeps what they typed", async () => {
    stubApi(() =>
      jsonResponse(400, {
        error: { code: "invalid_request", message: "unknown group 'ghosts'" },
      }),
    );
    const { user, onChanged } = renderForm();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Who" }),
      "A group",
    );
    const name = screen.getByRole("textbox", { name: /Group name/ });
    await user.type(name, "ghosts");
    await user.click(screen.getByRole("button", { name: "Share" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The access was not changed");
    expect(alert).toHaveTextContent("unknown group 'ghosts'");
    expect(name).toHaveValue("ghosts");
    expect(onChanged).not.toHaveBeenCalled();
  });
});
