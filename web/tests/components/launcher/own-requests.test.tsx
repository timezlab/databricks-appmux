import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OwnRequests } from "@/components/launcher/own-requests";
import { API_BASE_URL, RequestList } from "@/lib/api";
import type { RequestRow } from "@/lib/api";
import type { OwnRequestsSection } from "@/lib/launcher/requests";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

afterEach(() => {
  vi.restoreAllMocks();
});

// In the contract's example, frank asked for requests 5 (delegation, pending),
// 6 (new app wiki, pending) and 8 (settings, denied); dana and alice asked for
// the others.
const EXAMPLE_REQUESTS = RequestList.parse(readExample("RequestList"));
const WIKI_REQUEST_ID = 6;

function section(email: string): OwnRequestsSection {
  return {
    status: "ready",
    email,
    items: EXAMPLE_REQUESTS.items,
    stale: null,
  };
}

function renderRequests(email: string) {
  const onWithdrawn = vi.fn();
  render(
    <OwnRequests
      section={section(email)}
      onWithdrawn={onWithdrawn}
      onReload={vi.fn()}
    />,
  );
  return { onWithdrawn, user: userEvent.setup() };
}

function withdrawnWiki(): RequestRow {
  const pending = EXAMPLE_REQUESTS.items.find(
    (request) => request.request_id === WIKI_REQUEST_ID,
  );
  if (pending === undefined) throw new Error("example lost request 6");
  return {
    ...pending,
    status: "withdrawn",
    closed_by: pending.requested_by,
    closed_at: "2026-10-31T08:00:00Z",
  };
}

describe("OwnRequests", () => {
  it("lists only the visitor's own requests and offers Withdraw on the pending ones", () => {
    renderRequests("frank@example.com");

    expect(screen.getAllByRole("row")).toHaveLength(1 + 3);
    expect(screen.getAllByRole("button", { name: /^Withdraw/ })).toHaveLength(
      2,
    );
    expect(screen.queryByText(/Settings change for sales/)).toBeNull();
    const denied = screen.getByRole("row", {
      name: /Settings change for docs/,
    });
    expect(
      within(denied).queryByRole("button", { name: /^Withdraw/ }),
    ).toBeNull();
  });

  it("withdraws through the API, shows the row as withdrawn at once and reports it", async () => {
    const calls: RecordedCall[] = [];
    stubFetch((call) => {
      calls.push(call);
      return jsonResponse(200, withdrawnWiki());
    });
    const { user, onWithdrawn } = renderRequests("frank@example.com");

    await user.click(
      screen.getByRole("button", { name: /^Withdraw\s*new app wiki$/ }),
    );

    await waitFor(() => {
      expect(onWithdrawn).toHaveBeenCalledTimes(1);
    });
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(
      `${API_BASE_URL}/requests/${WIKI_REQUEST_ID}/withdraw`,
    );
    expect(calls[0]?.init.method).toBe("POST");
    expect(onWithdrawn).toHaveBeenCalledWith(
      expect.objectContaining({
        request_id: WIKI_REQUEST_ID,
        status: "withdrawn",
      }),
    );
    expect(
      screen.queryByRole("button", { name: /^Withdraw\s*new app wiki$/ }),
    ).toBeNull();
    expect(
      within(screen.getByRole("row", { name: /New app: Team wiki/ })).getByText(
        /Withdrawn/,
      ),
    ).toBeInTheDocument();
  });

  it("tells the person when the withdrawal failed and leaves the request pending", async () => {
    stubFetch(() =>
      jsonResponse(409, {
        error: { code: "not_pending", message: "the request is not pending" },
      }),
    );
    const { user, onWithdrawn } = renderRequests("frank@example.com");

    await user.click(
      screen.getByRole("button", { name: /^Withdraw\s*new app wiki$/ }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not withdraw the request",
    );
    expect(onWithdrawn).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: /^Withdraw\s*new app wiki$/ }),
    ).toBeInTheDocument();
  });

  it("says so when the visitor has made no request", () => {
    renderRequests("nobody@example.com");

    expect(
      screen.getByText("You have not made any requests"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
