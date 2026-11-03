import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminsSection } from "@/components/platform/admins-section";
import { PlatformProvider } from "@/components/platform/platform-context";
import { PlatformView, PutAdminsRequest } from "@/lib/api";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

const BASE_VIEW = PlatformView.parse(readExample("PlatformView"));
const VIEW = {
  ...BASE_VIEW,
  admins: [
    { type: "user", name: "dana@example.com" },
    { type: "user", name: "erin@example.com" },
    { type: "group", name: "data-platform" },
  ],
} satisfies PlatformView;
const VIEWER = { email: "dana@example.com", groups: [] };

let calls: RecordedCall[] = [];

function bodyOf(call: RecordedCall): unknown {
  return typeof call.init.body === "string"
    ? (JSON.parse(call.init.body) as unknown)
    : null;
}

function sentBodies(): unknown[] {
  return writes().map(bodyOf);
}

function setup() {
  stubFetch((call) => {
    calls.push(call);
    if (call.url.includes("/principals")) {
      return jsonResponse(200, readExample("PrincipalList"));
    }
    const body = PutAdminsRequest.parse(bodyOf(call));
    return jsonResponse(200, { items: body.admins });
  });
  const refresh = vi.fn();
  const announce = vi.fn();
  const user = userEvent.setup();
  render(
    <PlatformProvider value={{ refresh, announce }}>
      <AdminsSection view={VIEW} viewer={VIEWER} />
    </PlatformProvider>,
  );
  return { user, refresh, announce };
}

/** The calls that changed the list; the field's own searches are not among them. */
function writes(): RecordedCall[] {
  return calls.filter((call) => !call.url.includes("/principals"));
}

function whoField(): HTMLElement {
  return screen.getByRole("combobox", { name: /^Who/ });
}

/** Opens the field, types `text`, and picks the row named `name`. */
async function pick(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
  name: RegExp,
): Promise<void> {
  await user.click(whoField());
  await user.type(screen.getByPlaceholderText(/^Type a name/), text);
  await user.click(await screen.findByRole("option", { name }));
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AdminsSection add form", () => {
  it("adds a group chosen from the directory's suggestions, and offers only users and groups", async () => {
    const { user, announce, refresh } = setup();

    await user.type(
      screen.getByLabelText(/^Reason for the next change/),
      "the sales team runs the platform",
    );
    await user.click(whoField());
    await user.type(screen.getByPlaceholderText(/^Type a name/), "sa");
    // Users and groups are suggested; a service principal and everyone are not.
    expect(
      await screen.findByRole("option", { name: /sales-team/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Erin Ng/ })).toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: /sales-agent/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: /Everyone/ }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("option", { name: /sales-team/ }));
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    expect(
      await screen.findByText("Added sales-team to the platform admins"),
    ).toBeInTheDocument();
    expect(sentBodies()).toEqual([
      {
        admins: [...VIEW.admins, { type: "group", name: "sales-team" }],
        reason: "the sales team runs the platform",
      },
    ]);
    expect(announce).toHaveBeenCalledWith(
      "Added sales-team to the platform admins",
    );
    expect(refresh).toHaveBeenCalledTimes(1);
    // The form is ready for the next change.
    expect(screen.getByLabelText(/^Reason for the next change/)).toHaveValue(
      "",
    );
    expect(whoField()).toHaveTextContent("Search users and groups");
  });

  it("takes a typed e-mail address as a user when the directory does not list it", async () => {
    const { user } = setup();

    await user.type(
      screen.getByLabelText(/^Reason for the next change/),
      "new on call",
    );
    await pick(user, "frank@example.com", /Use .*frank@example\.com/);
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    expect(
      await screen.findByText("Added frank@example.com to the platform admins"),
    ).toBeInTheDocument();
    expect(sentBodies()).toEqual([
      {
        admins: [...VIEW.admins, { type: "user", name: "frank@example.com" }],
        reason: "new on call",
      },
    ]);
  });

  it("does not offer a service principal id typed in full: only users and groups can be admins", async () => {
    const { user } = setup();

    await user.click(whoField());
    await user.type(
      screen.getByPlaceholderText(/^Type a name/),
      "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab",
    );

    // The answer has arrived (the stub returns the same list whatever is typed).
    await screen.findByRole("option", { name: /sales-team/ });
    expect(
      screen.queryByRole("option", { name: /^Use/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: /sales-agent/ }),
    ).not.toBeInTheDocument();
  });

  it("refuses a name already listed under the same type, whatever its case, and sends nothing", async () => {
    const { user } = setup();

    await user.type(
      screen.getByLabelText(/^Reason for the next change/),
      "again",
    );
    await pick(user, "Data-Platform", /Use .*Data-Platform/);
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    const who = whoField();
    expect(who).toBeInvalid();
    expect(who).toHaveAccessibleDescription(/is already a platform admin/);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Some fields need attention",
    );
    expect(writes()).toHaveLength(0);
  });

  it("asks who to add before anything is sent", async () => {
    const { user } = setup();

    await user.type(
      screen.getByLabelText(/^Reason for the next change/),
      "no one chosen",
    );
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    expect(whoField()).toBeInvalid();
    expect(whoField()).toHaveAccessibleDescription(
      /Choose a user or a group to add\./,
    );
    expect(writes()).toHaveLength(0);
  });

  it("asks for a reason before anything is sent", async () => {
    const { user } = setup();

    await pick(user, "frank@example.com", /Use .*frank@example\.com/);
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    expect(
      screen.getByLabelText(/^Reason for the next change/),
    ).toHaveAccessibleDescription(/must not be blank/);
    expect(writes()).toHaveLength(0);
  });
});

describe("AdminsSection remove", () => {
  it("asks first, warns when the entry is the signed-in person, and sends the list without the entry once confirmed", async () => {
    const { user } = setup();
    await user.type(
      screen.getByLabelText(/^Reason for the next change/),
      "left the team",
    );

    const own = within(screen.getByRole("row", { name: /dana@example\.com/ }));
    await user.click(own.getByRole("button", { name: "Remove" }));
    expect(
      screen.getByRole("group", {
        name: "Remove dana@example.com? You would lose access to this page.",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(writes()).toHaveLength(0);

    const erin = within(screen.getByRole("row", { name: /erin@example\.com/ }));
    await user.click(erin.getByRole("button", { name: "Remove" }));
    expect(writes()).toHaveLength(0);
    await user.click(
      screen.getByRole("button", { name: "Yes, remove erin@example.com" }),
    );

    expect(
      await screen.findByText(
        "Removed erin@example.com from the platform admins",
      ),
    ).toBeInTheDocument();
    expect(sentBodies()).toEqual([
      {
        admins: [VIEW.admins[0], VIEW.admins[2]],
        reason: "left the team",
      },
    ]);
  });
});
