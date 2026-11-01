import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DelegationsSection } from "@/components/platform/delegations-section";
import { PlatformProvider } from "@/components/platform/platform-context";
import { PlatformView } from "@/lib/api";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

// Contract example: `sales` holds OBO (app version 3), `docs` holds nothing
// (version 2), and the shared-SP master switch is off.
const VIEW = PlatformView.parse(readExample("PlatformView"));
const OBO = "On behalf of the visitor (OBO)";
const SHARED_SP = "Shared service principal";

let calls: RecordedCall[] = [];

function bodyOf(call: RecordedCall | undefined): unknown {
  return typeof call?.init.body === "string"
    ? (JSON.parse(call.init.body) as unknown)
    : null;
}

function setup() {
  stubFetch((call) => {
    calls.push(call);
    return jsonResponse(200, readExample("AppDetail"));
  });
  const refresh = vi.fn();
  const announce = vi.fn();
  const user = userEvent.setup();
  render(
    <PlatformProvider value={{ refresh, announce }}>
      <DelegationsSection view={VIEW} />
    </PlatformProvider>,
  );
  return { user, refresh };
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DelegationsSection", () => {
  it("asks for a reason, then grants with the latest app version and closes the form", async () => {
    const { user, refresh } = setup();

    await user.click(
      screen.getByRole("button", { name: `Grant ${OBO} for docs` }),
    );
    await user.click(screen.getByRole("button", { name: "Grant" }));

    expect(screen.getByLabelText(/^Reason/)).toHaveAccessibleDescription(
      /must not be blank/,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Some fields need attention",
    );
    expect(calls).toHaveLength(0);

    await user.type(
      screen.getByLabelText(/^Reason/),
      " the docs need the visitor ",
    );
    await user.click(screen.getByRole("button", { name: "Grant" }));

    expect(
      await screen.findByText(`Allowed ${OBO} for docs`),
    ).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.init.method).toBe("PUT");
    expect(calls[0]?.url).toMatch(/\/apps\/docs\/delegation\/obo$/);
    expect(bodyOf(calls[0])).toEqual({
      version: 2,
      reason: "the docs need the visitor",
    });
    expect(
      screen.queryByRole("heading", { name: `Grant ${OBO} for docs` }),
    ).not.toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("revokes through a DELETE that carries the reason and the version in the query", async () => {
    const { user } = setup();

    await user.click(
      screen.getByRole("button", { name: `Revoke ${OBO} for sales` }),
    );
    await user.type(screen.getByLabelText(/^Reason/), "no longer needed");
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    expect(
      await screen.findByText(`Revoked ${OBO} for sales`),
    ).toBeInTheDocument();
    expect(calls[0]?.init.method).toBe("DELETE");
    expect(calls[0]?.url).toMatch(
      /\/apps\/sales\/delegation\/obo\?reason=no%20longer%20needed&version=3$/,
    );
  });

  it("offers no grant of the shared service principal while its master switch is off", () => {
    setup();

    expect(
      screen.queryByRole("button", { name: `Grant ${SHARED_SP} for docs` }),
    ).not.toBeInTheDocument();
    expect(
      screen.getAllByText(
        "Turn on the shared_sp_delegation_allowed setting before granting this.",
      ),
    ).toHaveLength(3);
  });
});
