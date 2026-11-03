import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InboxSection } from "@/components/platform/inbox-section";
import { PlatformProvider } from "@/components/platform/platform-context";
import { PlatformView, RequestRow } from "@/lib/api";
import type { RequestList } from "@/lib/api";
import type { PollResult } from "@/lib/poll/schedule";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";
import { readExample } from "../../support/contract-examples";

const VIEW = PlatformView.parse(readExample("PlatformView"));

const SETTINGS_REQUEST = RequestRow.parse(readExample("RequestRow"));

const REGISTER_REQUEST = RequestRow.parse({
  ...SETTINGS_REQUEST,
  request_id: 5,
  app_slug: null,
  kind: "register",
  payload: {
    slug: "hr",
    display_name: "HR portal",
    source_path: "/Workspace/Shared/apps/hr",
  },
  reason: "the people team needs a leave calendar",
  requested_by: "erin@example.com",
  current: null,
  requires_reason: true,
});

/** What the registry holds; the stub answers a decision by closing the request here. */
let serverRows: RequestRow[] = [];
let calls: RecordedCall[] = [];

function bodyOf(call: RecordedCall | undefined): unknown {
  return typeof call?.init.body === "string"
    ? (JSON.parse(call.init.body) as unknown)
    : null;
}

function stubDecisions(failWith?: { status: number; code: string }): void {
  stubFetch((call) => {
    calls.push(call);
    if (failWith !== undefined) {
      return jsonResponse(failWith.status, {
        error: { code: failWith.code, message: "the request is closed" },
      });
    }
    const match = /\/requests\/(\d+)\/(approve|deny)$/.exec(call.url);
    const row = serverRows.find(
      (candidate) => String(candidate.request_id) === match?.[1],
    );
    if (match === null || row === undefined) return jsonResponse(404, {});
    const decided = {
      ...row,
      status: match[2] === "approve" ? "approved" : "denied",
    } as const;
    serverRows = serverRows.map((candidate) =>
      candidate === row ? decided : candidate,
    );
    return jsonResponse(200, decided);
  });
}

/** Stands in for the platform page: `refresh` fetches the list again, as the poll does after a write. */
function Harness({ announce }: { announce: (message: string) => void }) {
  const [requests, setRequests] = useState<PollResult<RequestList>>({
    status: "ready",
    data: { items: serverRows },
    updatedAtMs: 0,
  });
  const refresh = () => {
    setRequests({
      status: "ready",
      data: { items: serverRows },
      updatedAtMs: 1,
    });
  };
  return (
    <PlatformProvider value={{ refresh, announce }}>
      <InboxSection requests={requests} view={VIEW} />
    </PlatformProvider>
  );
}

function setup(
  rows: RequestRow[],
  failWith?: { status: number; code: string },
) {
  serverRows = rows;
  stubDecisions(failWith);
  const announce = vi.fn();
  const user = userEvent.setup();
  render(<Harness announce={announce} />);
  return { user, announce };
}

function card(id: number) {
  const heading = screen.getByRole("heading", {
    name: new RegExp(`^Request #${id}:`),
  });
  const article = heading.closest("article");
  if (article === null) throw new Error(`Request #${id} has no card`);
  return within(article);
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("InboxSection", () => {
  it("approves the request whose card was used, with the reason and the note, and the card leaves the list", async () => {
    const { user, announce } = setup([SETTINGS_REQUEST, REGISTER_REQUEST]);

    // A registration holds a gate field, so approving it needs a reason first.
    await user.click(card(5).getByRole("button", { name: "Approve" }));
    expect(calls).toHaveLength(0);
    expect(card(5).getByLabelText(/^Reason/)).toHaveAccessibleDescription(
      /is required to approve this request/,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Some fields need attention",
    );

    await user.type(card(5).getByLabelText(/^Reason/), " leave calendar ");
    await user.type(card(5).getByLabelText("Note to the asker"), "welcome");
    await user.click(card(5).getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("Approved request #5")).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toMatch(/\/requests\/5\/approve$/);
    expect(calls[0]?.init.method).toBe("POST");
    expect(bodyOf(calls[0])).toEqual({
      reason: "leave calendar",
      note: "welcome",
    });
    expect(announce).toHaveBeenCalledWith("Approved request #5");

    // The other request was not touched and is still waiting.
    expect(
      screen.queryByRole("heading", { name: /^Request #5:/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /^Request #4:/ }),
    ).toBeInTheDocument();
  });

  it("shows a registration with the platform default as its memory budget, not as missing", () => {
    setup([REGISTER_REQUEST]);

    expect(VIEW.settings.default_memory_budget_mb).toBe(512);
    expect(card(5).getByText("512 MB")).toBeInTheDocument();
    expect(card(5).queryByText("(missing)")).not.toBeInTheDocument();
  });

  it("asks before it denies, and Cancel sends nothing", async () => {
    const { user } = setup([SETTINGS_REQUEST]);

    await user.click(card(4).getByRole("button", { name: "Deny" }));
    expect(calls).toHaveLength(0);
    expect(
      card(4).getByRole("group", { name: /^Deny request #4\?/ }),
    ).toBeInTheDocument();

    await user.click(card(4).getByRole("button", { name: "Cancel" }));
    expect(calls).toHaveLength(0);
    expect(card(4).getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });

  it("denies through the deny route of that request, without a reason, once confirmed, and the card leaves the list", async () => {
    const { user } = setup([SETTINGS_REQUEST, REGISTER_REQUEST]);

    await user.click(card(4).getByRole("button", { name: "Deny" }));
    await user.click(
      card(4).getByRole("button", { name: "Yes, deny request #4" }),
    );

    expect(await screen.findByText("Denied request #4")).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toMatch(/\/requests\/4\/deny$/);
    // Blank fields are left out; denying never needs a reason.
    expect(bodyOf(calls[0])).toEqual({});
    expect(
      screen.queryByRole("heading", { name: /^Request #4:/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /^Request #5:/ }),
    ).toBeInTheDocument();
  });

  it("keeps the request and shows the error when the server refuses the answer", async () => {
    const { user } = setup([SETTINGS_REQUEST], {
      status: 409,
      code: "request_closed",
    });

    await user.click(card(4).getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not answer the request",
    );
    expect(
      screen.getByRole("heading", { name: /^Request #4:/ }),
    ).toBeInTheDocument();
  });

  it("shows what the asker wrote as text, with hidden characters spelled out", () => {
    const hostile = RequestRow.parse({
      ...SETTINGS_REQUEST,
      reason: "<img src=x onerror=alert(1)>\n<b>second</b> line\u202Eevil",
      requested_by: "<script>alert(2)</script>@example.com",
    });
    setup([hostile]);

    expect(
      screen.getByText("<img src=x onerror=alert(1)>"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("<b>second</b> line<U+202E>evil"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("<script>alert(2)</script>@example.com"),
    ).toBeInTheDocument();
    const article = screen.getByRole("article");
    expect(article.querySelector("img, b, script")).toBeNull();
  });

  it("says so when nothing is waiting", () => {
    setup([{ ...SETTINGS_REQUEST, status: "withdrawn" }]);
    expect(screen.getByText("No pending requests")).toBeInTheDocument();
  });
});
