import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminsSection } from "@/components/platform/admins-section";
import { PlatformProvider } from "@/components/platform/platform-context";
import { PlatformView, PutAdminsRequest } from "@/lib/api";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";
import { chooseOption } from "../../support/select";

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
  return calls.map(bodyOf);
}

function setup() {
  stubFetch((call) => {
    calls.push(call);
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

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AdminsSection add form", () => {
  it("lists a group named like a listed user as a new entry, because the type is compared too", async () => {
    const { user, announce, refresh } = setup();

    await user.type(
      screen.getByLabelText(/^Reason for the next change/),
      "mirror Dana as a group",
    );
    await chooseOption(user, screen.getByLabelText("Type"), "Group");
    await user.type(screen.getByLabelText("Group name"), " dana@example.com ");
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    expect(
      await screen.findByText("Added dana@example.com to the platform admins"),
    ).toBeInTheDocument();
    expect(sentBodies()).toEqual([
      {
        admins: [...VIEW.admins, { type: "group", name: "dana@example.com" }],
        reason: "mirror Dana as a group",
      },
    ]);
    expect(announce).toHaveBeenCalledWith(
      "Added dana@example.com to the platform admins",
    );
    expect(refresh).toHaveBeenCalledTimes(1);
    // The form is ready for the next change.
    expect(screen.getByLabelText(/^Reason for the next change/)).toHaveValue(
      "",
    );
    expect(screen.getByLabelText("Group name")).toHaveValue("");
  });

  it("refuses a name already listed under the same type, whatever its case, and sends nothing", async () => {
    const { user } = setup();

    await user.type(
      screen.getByLabelText(/^Reason for the next change/),
      "again",
    );
    await chooseOption(user, screen.getByLabelText("Type"), "Group");
    await user.type(screen.getByLabelText("Group name"), "Data-Platform");
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    const name = screen.getByLabelText("Group name");
    expect(name).toBeInvalid();
    expect(name).toHaveAccessibleDescription(/is already a platform admin/);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Some fields need attention",
    );
    expect(calls).toHaveLength(0);
  });

  it("asks for a reason before anything is sent", async () => {
    const { user } = setup();

    await user.type(
      screen.getByLabelText("Email address"),
      "frank@example.com",
    );
    await user.click(screen.getByRole("button", { name: "Add admin" }));

    expect(
      screen.getByLabelText(/^Reason for the next change/),
    ).toHaveAccessibleDescription(/must not be blank/);
    expect(calls).toHaveLength(0);
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
    expect(calls).toHaveLength(0);

    const erin = within(screen.getByRole("row", { name: /erin@example\.com/ }));
    await user.click(erin.getByRole("button", { name: "Remove" }));
    expect(calls).toHaveLength(0);
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
