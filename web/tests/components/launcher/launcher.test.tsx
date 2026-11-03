import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Launcher } from "@/components/launcher/launcher";
import { AppList, MeResponse } from "@/lib/api";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

afterEach(() => {
  vi.restoreAllMocks();
});

const EXAMPLE_ME = MeResponse.parse(readExample("MeResponse"));
const EXAMPLE_APPS = AppList.parse(readExample("AppList"));

// The example requests that frank made show "Your requests" once `GET /me` has
// answered, which tells the tests below that the answer is in.
const REQUESTER_WITH_REQUESTS = "frank@example.com";

function answerAs({
  isPlatformAdmin,
  permission,
}: {
  isPlatformAdmin: boolean;
  permission: "CAN_USE" | "CAN_MANAGE";
}): void {
  stubFetch(({ url }: RecordedCall): Response => {
    if (url.endsWith("/api/v1/me"))
      return jsonResponse(200, {
        ...EXAMPLE_ME,
        email: REQUESTER_WITH_REQUESTS,
        is_platform_admin: isPlatformAdmin,
      });
    if (url.endsWith("/api/v1/apps"))
      return jsonResponse(200, {
        items: EXAMPLE_APPS.items.map((app) => ({ ...app, permission })),
      });
    if (url.endsWith("/api/v1/requests"))
      return jsonResponse(200, readExample("RequestList"));
    return jsonResponse(404, {
      error: { code: "not_found", message: "no such route in this test" },
    });
  });
}

async function openTheRequestForm(): Promise<void> {
  const user = userEvent.setup();
  render(<Launcher />);
  await screen.findByRole("heading", { name: "Your requests" });
  await user.click(screen.getByRole("button", { name: "Request a new app" }));
  await screen.findByRole("textbox", { name: /^Source folder/ });
}

describe("the launcher's request form offers Browse only to who may list the workspace", () => {
  it("offers it to a platform admin", async () => {
    answerAs({ isPlatformAdmin: true, permission: "CAN_USE" });
    await openTheRequestForm();
    expect(screen.getByRole("button", { name: "Browse" })).toBeInTheDocument();
  });

  it("offers it to a person who manages an app", async () => {
    answerAs({ isPlatformAdmin: false, permission: "CAN_MANAGE" });
    await openTheRequestForm();
    expect(screen.getByRole("button", { name: "Browse" })).toBeInTheDocument();
  });

  it("leaves it out for a person who only uses apps, who types the path", async () => {
    answerAs({ isPlatformAdmin: false, permission: "CAN_USE" });
    await openTheRequestForm();
    expect(
      screen.queryByRole("button", { name: "Browse" }),
    ).not.toBeInTheDocument();
  });
});
