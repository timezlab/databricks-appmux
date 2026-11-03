import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PrincipalCombobox } from "@/components/principal-combobox";
import type { PrincipalChoice } from "@/lib/app/sharing";
import {
  describeCall,
  jsonResponse,
  readExample,
  stubApi,
} from "./app/form-test-harness";

afterEach(() => {
  vi.restoreAllMocks();
});

const SP_ID = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab";

function renderCombobox(initial: PrincipalChoice | null = null) {
  const onValueChange = vi.fn<(choice: PrincipalChoice | null) => void>();
  function Host() {
    const [value, setValue] = useState<PrincipalChoice | null>(initial);
    return (
      <>
        <label htmlFor="who">Who</label>
        <PrincipalCombobox
          id="who"
          value={value}
          onValueChange={(choice) => {
            setValue(choice);
            onValueChange(choice);
          }}
        />
      </>
    );
  }
  render(<Host />);
  return { onValueChange, user: userEvent.setup() };
}

function trigger() {
  return screen.getByRole("combobox", { name: /Who/ });
}

function searchBox() {
  return screen.getByPlaceholderText(/Type a name/);
}

function queryOf(call: Parameters<typeof describeCall>[0]): string | null {
  return new URL(describeCall(call).path, "http://x").searchParams.get("q");
}

describe("PrincipalCombobox", () => {
  it("shows the directory's rows for what is typed, with the identifier beside the name, and picks one", async () => {
    const calls = stubApi(() =>
      jsonResponse(200, readExample("PrincipalList")),
    );
    const { user, onValueChange } = renderCombobox();

    await user.click(trigger());
    await user.type(searchBox(), "er");

    const erin = await screen.findByRole("option", { name: /Erin Ng/ });
    expect(erin).toHaveTextContent("erin@example.com");
    expect(
      screen.getByRole("option", { name: /sales-agent/ }),
    ).toHaveTextContent(SP_ID);
    // One call for the opened list, then one for the text typed at speed.
    await waitFor(() => {
      expect(queryOf(calls.at(-1))).toBe("er");
    });
    expect(calls.length).toBeLessThanOrEqual(2);

    await user.click(erin);
    expect(onValueChange).toHaveBeenLastCalledWith({
      type: "user",
      name: "erin@example.com",
      label: "Erin Ng",
      identifier: "erin@example.com",
    });
    expect(trigger()).toHaveTextContent("Erin Ng");
  });

  it("says quietly which directories are unavailable, and still takes the typed e-mail", async () => {
    stubApi(() =>
      jsonResponse(200, {
        items: [],
        truncated: false,
        sources: {
          users: "unavailable",
          groups: "unavailable",
          service_principals: "platform_token",
        },
      }),
    );
    const { user, onValueChange } = renderCombobox();

    await user.click(trigger());
    await user.type(searchBox(), "Dana@Example.com");

    expect(
      await screen.findByText(
        /Suggestions for users and groups are unavailable/,
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("option", { name: /Use “Dana@Example.com” as a user/ }),
    );
    expect(onValueChange).toHaveBeenLastCalledWith({
      type: "user",
      name: "dana@example.com",
      label: "dana@example.com",
      identifier: null,
    });
  });

  it("keeps free typing when the call fails, inferring a service principal from a UUID", async () => {
    stubApi(() =>
      jsonResponse(503, { code: "unavailable", message: "Try later" }),
    );
    const { user, onValueChange } = renderCombobox();

    await user.click(trigger());
    await user.type(searchBox(), SP_ID);

    expect(
      await screen.findByText(/Suggestions are unavailable/),
    ).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(onValueChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ type: "service_principal", name: SP_ID }),
    );
  });

  it("does not send what the server cannot search, and offers * as everyone", async () => {
    const calls = stubApi(() =>
      jsonResponse(200, readExample("PrincipalList")),
    );
    const { user, onValueChange } = renderCombobox();

    await user.click(trigger());
    await user.type(searchBox(), "*");
    await user.click(
      screen.getByRole("option", { name: /Use “\*” as everyone/ }),
    );

    expect(calls.map(queryOf)).not.toContain("*");
    expect(onValueChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ type: "everyone", name: "*" }),
    );
  });

  it("shows the chosen principal as a chip, and clearing it puts focus back on the field", async () => {
    stubApi(() => jsonResponse(200, readExample("PrincipalList")));
    const { user, onValueChange } = renderCombobox({
      type: "group",
      name: "sales-team",
      label: "sales-team",
      identifier: null,
    });

    expect(trigger()).toHaveTextContent("sales-team");
    await user.click(screen.getByRole("button", { name: "Clear sales-team" }));

    expect(onValueChange).toHaveBeenLastCalledWith(null);
    expect(trigger()).toHaveFocus();
    expect(
      screen.queryByRole("button", { name: /Clear/ }),
    ).not.toBeInTheDocument();
  });
});
