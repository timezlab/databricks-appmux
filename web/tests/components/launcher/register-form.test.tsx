import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserEvent } from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RegisterForm } from "@/components/launcher/register-form";
import { API_BASE_URL } from "@/lib/api";
import {
  readExample,
  readExampleObject,
} from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

afterEach(() => {
  vi.restoreAllMocks();
});

const NOTHING_KNOWN = { knownSlugs: [], pendingRegisterSlugs: [] };

function renderForm() {
  const onSent = vi.fn();
  const onReload = vi.fn();
  render(
    <RegisterForm
      context={NOTHING_KNOWN}
      onSent={onSent}
      onCancel={vi.fn()}
      onReload={onReload}
    />,
  );
  return { onSent, onReload, user: userEvent.setup() };
}

// The contract's own example of a register request: what a valid form holds.
const VALID_REQUEST = readExampleObject("CreatePermissionRequest");

function sentJson(call: RecordedCall | undefined): unknown {
  const body = call?.init.body;
  if (typeof body !== "string") throw new Error("the call has no JSON body");
  return JSON.parse(body);
}

async function fillValidForm(user: UserEvent, slug = "wiki"): Promise<void> {
  await user.type(screen.getByRole("textbox", { name: /^Slug/ }), slug);
  await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Team wiki");
  await user.type(
    screen.getByRole("textbox", { name: /^Source folder/ }),
    "/Workspace/Shared/apps/wiki",
  );
  await user.type(
    screen.getByRole("textbox", { name: /^Memory budget/ }),
    "128",
  );
  await user.type(
    screen.getByRole("textbox", { name: /^Reason/ }),
    "the team wiki, a small Flask app",
  );
}

describe("RegisterForm", () => {
  it("opens with the keyboard on the slug field", () => {
    renderForm();
    expect(screen.getByRole("textbox", { name: /^Slug/ })).toHaveFocus();
  });

  it("marks an invalid slug, announces the failure, moves focus back to it and sends nothing", async () => {
    const calls: RecordedCall[] = [];
    stubFetch((call) => {
      calls.push(call);
      return jsonResponse(201, readExample("RequestRow"));
    });
    const { user, onSent } = renderForm();
    await fillValidForm(user, "Bad Slug!");

    await user.click(screen.getByRole("button", { name: "Send request" }));

    const slug = screen.getByRole("textbox", { name: /^Slug/ });
    expect(slug).toBeInvalid();
    expect(slug).toHaveAccessibleDescription(
      /Error:\s*Use 1 to 31 lower-case letters, digits or hyphens/,
    );
    expect(slug).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The request was not sent",
    );
    expect(calls).toHaveLength(0);
    expect(onSent).not.toHaveBeenCalled();
  });

  it("drops a field's complaint as soon as the person edits that field", async () => {
    stubFetch(() => jsonResponse(201, readExample("RequestRow")));
    const { user } = renderForm();

    await user.click(screen.getByRole("button", { name: "Send request" }));
    expect(screen.getByRole("textbox", { name: /^Slug/ })).toBeInvalid();
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeInvalid();

    await user.type(screen.getByRole("textbox", { name: /^Slug/ }), "w");

    expect(screen.getByRole("textbox", { name: /^Slug/ })).toBeValid();
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeInvalid();
  });

  it("sends the request body of the contract on a valid submit, then reports the saved row", async () => {
    const calls: RecordedCall[] = [];
    stubFetch((call) => {
      calls.push(call);
      return jsonResponse(201, readExample("RequestRow"));
    });
    const { user, onSent } = renderForm();
    await fillValidForm(user);

    await user.click(screen.getByRole("button", { name: "Send request" }));

    await waitFor(() => {
      expect(onSent).toHaveBeenCalledTimes(1);
    });
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(`${API_BASE_URL}/requests`);
    expect(calls[0]?.init.method).toBe("POST");
    expect(sentJson(calls[0])).toEqual(VALID_REQUEST);
    expect(onSent).toHaveBeenCalledWith(
      expect.objectContaining({ request_id: 4 }),
    );
  });

  it("shows a busy button while the call runs and sends only once", async () => {
    let answer: (response: Response) => void = () => undefined;
    const calls: RecordedCall[] = [];
    stubFetch((call) => {
      calls.push(call);
      return new Promise<Response>((resolve) => {
        answer = resolve;
      });
    });
    const { user, onSent } = renderForm();
    await fillValidForm(user);

    await user.click(screen.getByRole("button", { name: "Send request" }));

    const busy = await screen.findByRole("button", { name: "Sending" });
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toHaveAttribute("aria-disabled", "true");

    await user.click(busy);
    await user.keyboard("{Enter}");
    expect(calls).toHaveLength(1);

    answer(jsonResponse(201, readExample("RequestRow")));
    await waitFor(() => {
      expect(onSent).toHaveBeenCalledTimes(1);
    });
  });

  it("shows what the server refused, keeps the typed values and offers a reload after a conflict", async () => {
    stubFetch(() =>
      jsonResponse(409, {
        error: { code: "slug_taken", message: "slug 'wiki' is already taken" },
      }),
    );
    const { user, onSent, onReload } = renderForm();
    await fillValidForm(user);

    await user.click(screen.getByRole("button", { name: "Send request" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The request was not sent");
    expect(alert).toHaveTextContent("slug 'wiki' is already taken");
    expect(screen.getByRole("textbox", { name: /^Slug/ })).toHaveValue("wiki");
    expect(onSent).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });
});
