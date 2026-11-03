import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GrantForm } from "@/components/app/grant-form";
import { stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";
import {
  APP,
  describeCall,
  jsonResponse,
  readExample,
  renderWithActions,
} from "./form-test-harness";

afterEach(() => {
  vi.restoreAllMocks();
});

const SP_ID = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab";

/**
 * The directory answers with the contract's example list; a grant is
 * answered by `onGrant`. Only the grant calls are returned.
 */
function stubDirectoryAndGrant(
  onGrant: () => Response | Promise<Response> = () =>
    jsonResponse(200, readExample("AppGrant")),
): RecordedCall[] {
  const grantCalls: RecordedCall[] = [];
  stubFetch((call) => {
    if (call.url.includes("/principals")) {
      return jsonResponse(200, readExample("PrincipalList"));
    }
    grantCalls.push(call);
    return onGrant();
  });
  return grantCalls;
}

function renderForm() {
  const onReload = vi.fn();
  const view = renderWithActions((actions) => (
    <GrantForm app={APP} actions={actions} onReload={onReload} />
  ));
  return { ...view, onReload, user: userEvent.setup() };
}

function who() {
  return screen.getByRole("combobox", { name: /^Who/ });
}

function level() {
  return screen.getByRole("combobox", { name: /^Level/ });
}

async function pick(
  user: ReturnType<typeof userEvent.setup>,
  typed: string,
  option: RegExp,
) {
  await user.click(who());
  await user.type(screen.getByPlaceholderText(/Type a name/), typed);
  await user.click(await screen.findByRole("option", { name: option }));
}

describe("GrantForm", () => {
  it("adds a suggested user at the chosen level, then clears the field and puts focus back on it", async () => {
    const calls = stubDirectoryAndGrant();
    const { user, onChanged, onNotice } = renderForm();

    await pick(user, "erin", /Erin Ng/);
    await user.click(level());
    await user.click(screen.getByRole("option", { name: /^Can manage/ }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "PUT",
      path: "/apps/sales/grants",
      body: {
        type: "user",
        name: "erin@example.com",
        permission: "CAN_MANAGE",
      },
    });
    expect(onNotice).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "success" }),
    );
    expect(who()).not.toHaveTextContent("Erin Ng");
    expect(level()).toHaveTextContent("Can use");
    expect(who()).toHaveFocus();
  });

  it("refuses to add nobody: an announced error on the field, focus on it, nothing sent", async () => {
    const calls = stubDirectoryAndGrant();
    const { user } = renderForm();

    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(who()).toHaveAttribute("aria-invalid", "true");
    expect(who()).toHaveAccessibleDescription(/Choose who to share with/);
    expect(who()).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The access was not changed",
    );
    expect(calls).toHaveLength(0);
  });

  it("gives everyone only Can use: Can manage is off with the reason, and the schema's body is sent", async () => {
    const calls = stubDirectoryAndGrant();
    const { user, onChanged } = renderForm();

    await pick(user, "*", /Use “\*” as everyone/);

    expect(
      screen.getByText(/Everyone can only use the app/),
    ).toBeInTheDocument();
    await user.click(level());
    expect(screen.getByRole("option", { name: /^Can manage/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalled();
    });
    expect(describeCall(calls[0]).body).toEqual({
      type: "everyone",
      name: "*",
      permission: "CAN_USE",
    });
  });

  it("drops a chosen Can manage when a service principal is picked, which can only use the app", async () => {
    const calls = stubDirectoryAndGrant();
    const { user, onChanged } = renderForm();

    await user.click(level());
    await user.click(screen.getByRole("option", { name: /^Can manage/ }));
    await pick(user, "sales", /sales-agent/);

    expect(level()).toHaveTextContent("Can use");
    expect(screen.getByText(/ADR 0020/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalled();
    });
    expect(describeCall(calls[0]).body).toEqual({
      type: "service_principal",
      name: SP_ID,
      permission: "CAN_USE",
    });
  });

  it("is busy while the call runs: the button says so and a second press sends nothing", async () => {
    let answer: (response: Response) => void = () => undefined;
    const calls = stubDirectoryAndGrant(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    );
    const { user, onChanged } = renderForm();
    await pick(user, "sales", /sales-team/);

    await user.click(screen.getByRole("button", { name: "Add" }));
    const busy = await screen.findByRole("button", { name: "Adding" });
    expect(busy).toHaveAttribute("aria-busy", "true");
    await user.click(busy);
    expect(calls).toHaveLength(1);

    answer(jsonResponse(200, readExample("AppGrant")));
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByRole("button", { name: "Add" })).not.toHaveAttribute(
      "aria-busy",
    );
  });

  it("shows the server's refusal in words and keeps who was chosen", async () => {
    stubDirectoryAndGrant(() =>
      jsonResponse(400, {
        error: { code: "invalid_request", message: "unknown group 'ghosts'" },
      }),
    );
    const { user, onChanged } = renderForm();
    await pick(user, "ghosts", /Use “ghosts” as a group/);
    await user.click(screen.getByRole("button", { name: "Add" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The access was not changed");
    expect(alert).toHaveTextContent("unknown group 'ghosts'");
    expect(who()).toHaveTextContent("ghosts");
    expect(onChanged).not.toHaveBeenCalled();
  });
});
