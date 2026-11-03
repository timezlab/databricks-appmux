import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RequestsSection } from "@/components/app/requests-section";
import { ApiError, RequestList, RequestRow } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import type { PollResult } from "@/lib/poll/schedule";
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

const ALICE: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const CAROL: Actor = { ...ALICE, email: "carol@example.com" };

const EXAMPLE_LIST = RequestList.parse(readExample("RequestList"));
const PENDING_SETTINGS = RequestRow.parse(readExample("RequestRow"));

function ready(items: RequestRow[]): PollResult<RequestList> {
  return { status: "ready", data: { items }, updatedAtMs: 0 };
}

function renderSection(
  requests: PollResult<RequestList>,
  actor: Actor = ALICE,
) {
  const onReloadRequests = vi.fn();
  const view = renderWithActions((actions) => (
    <RequestsSection
      app={APP}
      actor={actor}
      actions={actions}
      requests={requests}
      onReloadRequests={onReloadRequests}
      onReload={vi.fn()}
    />
  ));
  return { ...view, onReloadRequests, user: userEvent.setup() };
}

function tableRows() {
  return within(
    screen.getByRole("table", { name: "Requests for this app" }),
  ).getAllByRole("row");
}

describe("RequestsSection", () => {
  it("lists this app's own requests with what was asked, why and a state word", () => {
    // The contract list also holds the docs app's requests and a registration.
    renderSection(ready(EXAMPLE_LIST.items));

    const rows = tableRows();
    expect(rows).toHaveLength(2); // header + the one request for `sales`
    expect(rows[1]).toHaveTextContent("Settings change");
    expect(rows[1]).toHaveTextContent("Slug: sales to revenue");
    expect(rows[1]).toHaveTextContent(
      "Reason: the dashboard now covers all revenue, not only sales",
    );
    expect(rows[1]).toHaveTextContent("Pending");
    expect(rows[1]).toHaveTextContent("Waiting for a platform admin");
  });

  it("shows each request's state as a lamp and its word", () => {
    const { container } = renderSection(ready([PENDING_SETTINGS]));
    const lamp = container.querySelector("[data-lamp]");
    expect(lamp).toHaveTextContent("Pending");
    expect(lamp).toHaveAttribute("data-lamp", "warning");
  });

  it("offers Withdraw to the asker of a pending request, asks first, and sends it by request id", async () => {
    const calls = stubApi(() =>
      jsonResponse(200, { ...PENDING_SETTINGS, status: "withdrawn" }),
    );
    const { user, onChanged } = renderSection(ready([PENDING_SETTINGS]));

    await user.click(screen.getByRole("button", { name: "Withdraw" }));
    expect(calls).toHaveLength(0);
    expect(
      screen.getByRole("group", {
        name: /Withdraw this settings change request\?/,
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Yes, withdraw it" }));

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "POST",
      path: "/requests/4/withdraw",
      body: undefined,
    });
  });

  it.each([
    ["a request someone else asked", CAROL, PENDING_SETTINGS],
    [
      "a request that was answered",
      ALICE,
      { ...PENDING_SETTINGS, status: "denied" as const },
    ],
  ])("offers no Withdraw for %s", (_name, actor, request) => {
    renderSection(ready([request]), actor);
    expect(screen.queryByRole("button", { name: /^Withdraw/ })).toBeNull();
  });

  it("does not let a second request of a kind be sent while one waits, and says why", async () => {
    const { user } = renderSection(ready([PENDING_SETTINGS]));

    const settings = screen.getByRole("button", {
      name: "Request a change of settings",
    });
    expect(settings).toBeDisabled();
    expect(
      screen.getByText(
        /Request a change of settings: A request of this kind is waiting/,
      ),
    ).toBeInTheDocument();

    const delegation = screen.getByRole("button", {
      name: "Request a delegation",
    });
    expect(delegation).toBeEnabled();
    expect(delegation).toHaveAttribute("aria-expanded", "false");
    await user.click(delegation);
    expect(delegation).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("form", { name: "Request a delegation" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("form", { name: "Request a delegation" }),
    ).toBeNull();
  });

  it("keeps both request buttons off while the list loads, so no refused request is sent", () => {
    renderSection({ status: "loading" });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading the requests",
    );
    expect(
      screen.getByRole("button", { name: "Request a delegation" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Request a change of settings" }),
    ).toBeDisabled();
  });

  it("shows a failed load with a way to try again", async () => {
    const { user, onReloadRequests } = renderSection({
      status: "failed",
      error: new ApiError(503, "unavailable", "registry is down"),
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not load the requests");
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(onReloadRequests).toHaveBeenCalledTimes(1);
  });

  it("shows the latest few and the rest on request", async () => {
    const many = Array.from({ length: 7 }, (_unused, index) => ({
      ...PENDING_SETTINGS,
      request_id: 100 + index,
      status: "denied" as const,
      closed_by: "dana@example.com",
      closed_at: "2026-10-30T10:20:00Z",
      closed_note: null,
    }));
    const { user } = renderSection(ready(many));
    expect(tableRows()).toHaveLength(1 + 5);

    await user.click(screen.getByRole("button", { name: "Show 2 older" }));
    expect(tableRows()).toHaveLength(1 + 7);

    await user.click(
      screen.getByRole("button", { name: "Show only the latest" }),
    );
    expect(tableRows()).toHaveLength(1 + 5);
  });
});
