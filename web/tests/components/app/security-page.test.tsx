import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppView } from "@/components/app/app-view";
import { AppSecurityPage } from "@/components/app/pages/security-page";
import { API_BASE_URL } from "@/lib/api";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";
import { describeCall } from "./form-test-harness";

vi.mock("next/navigation", () => ({
  usePathname: () => "/app/security/",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

interface Server {
  isPlatformAdmin?: boolean;
  app?: Record<string, unknown>;
  /** The answer to `GET /platform`; a promise that never settles keeps it loading. */
  platform?: () => Response | Promise<Response>;
}

function exampleApp(overrides: Record<string, unknown> = {}) {
  return {
    ...(readExample("AppDetail") as Record<string, unknown>),
    ...overrides,
  };
}

/** The app page's polls and `GET /platform` answer as the test says; every call is recorded. */
function serve(server: Server = {}): RecordedCall[] {
  const calls: RecordedCall[] = [];
  stubFetch((call) => {
    calls.push(call);
    const method = call.init.method ?? "GET";
    const path = call.url.replace(API_BASE_URL, "");
    if (method === "PUT" && path === "/apps/sales/egress") {
      const { body } = describeCall(call);
      const { rules } = body as { rules: unknown };
      return jsonResponse(200, exampleApp({ egress: rules, version: 4 }));
    }
    if (path === "/apps/sales")
      return jsonResponse(200, exampleApp(server.app));
    if (path === "/me")
      return jsonResponse(200, {
        ...(readExample("MeResponse") as Record<string, unknown>),
        is_platform_admin: server.isPlatformAdmin ?? true,
      });
    if (path === "/apps") return jsonResponse(200, readExample("AppList"));
    if (path === "/requests")
      return jsonResponse(200, readExample("RequestList"));
    if (path === "/platform")
      return (
        server.platform ??
        (() => jsonResponse(200, readExample("PlatformView")))
      )();
    return jsonResponse(404, {
      error: { code: "not_found", message: "no such route in this test" },
    });
  });
  return calls;
}

async function setup(server: Server = {}) {
  const calls = serve(server);
  const user = userEvent.setup();
  render(
    <AppView slug="sales">
      <AppSecurityPage />
    </AppView>,
  );
  await screen.findByRole("heading", { level: 2, name: "Egress rules" });
  return { user, calls };
}

function platformCalls(calls: readonly RecordedCall[]): RecordedCall[] {
  return calls.filter(({ url }) => url.endsWith("/api/v1/platform"));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AppSecurityPage, for an app admin", () => {
  it("lists the app's rules read-only, in words, with why they cannot be changed here", async () => {
    await setup({ isPlatformAdmin: false });
    const table = await screen.findByRole("table", {
      name: "Egress rules of sales",
    });
    const first = within(table).getByRole("row", { name: /^Rule 1/ });
    expect(within(first).getByRole("rowheader")).toHaveTextContent("Rule 1");
    expect(first).toHaveTextContent("10.20.0.0/16");
    expect(first).toHaveTextContent("port 443");
    const second = within(table).getByRole("row", { name: /^Rule 2/ });
    expect(second).toHaveTextContent("every address");
    expect(second).toHaveTextContent("any port");
    expect(
      screen.getByText(/Only a platform admin changes an app's egress rules/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Save rules" }),
    ).not.toBeInTheDocument();
  });

  it("shows the global rules as appmux ships them, without asking for the platform's settings", async () => {
    const { calls } = await setup({ isPlatformAdmin: false });
    const shipped = await screen.findByRole("table", {
      name: "The global rules as appmux ships them",
    });
    expect(shipped).toHaveTextContent("the container's own address");
    expect(shipped).toHaveTextContent("169.254.0.0/16");
    // An allow-list closed by `deny 0.0.0.0/0` stops before the global rules.
    expect(
      screen.getByText("Not reached for sales; shown for reference."),
    ).toBeInTheDocument();
    expect(platformCalls(calls)).toHaveLength(0);
  });

  it("says what decides when the app has no rules of its own", async () => {
    await setup({ isPlatformAdmin: false, app: { egress: [] } });
    expect(
      await screen.findByText(
        /sales has no rules of its own, as every new app/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("table", { name: "Egress rules of sales" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Not reached for sales; shown for reference."),
    ).not.toBeInTheDocument();
  });

  it("says what the sandbox forbids", async () => {
    await setup({ isPlatformAdmin: false });
    expect(
      screen.getByRole("heading", {
        level: 2,
        name: "What the sandbox forbids",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Files it can write")).toBeInTheDocument();
    expect(
      screen.getByText(/IPv6: appmux drops it/, { exact: false }),
    ).toBeInTheDocument();
  });
});

describe("AppSecurityPage, for a platform admin", () => {
  it("edits the app's rules and saves them with the app's version", async () => {
    const { user, calls } = await setup();
    const firstRule = await screen.findByRole("group", { name: "Rule 1" });
    const comment = within(firstRule).getByRole("textbox", { name: "Comment" });
    await user.clear(comment);
    await user.type(comment, "the warehouse");
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    expect(
      (await screen.findAllByText("Saved the egress rules of sales")).length,
    ).not.toBe(0);
    const put = describeCall(
      calls.find(({ init }) => (init.method ?? "GET") === "PUT"),
    );
    expect(put.path).toBe("/apps/sales/egress");
    const body = put.body as {
      version: number;
      rules: { comment: string }[];
    };
    expect(body.version).toBe(3);
    expect(body.rules[0]?.comment).toBe("the warehouse");
  });

  it("shows the global rules as stored, and a table-shaped skeleton until they arrive", async () => {
    let answer: (response: Response) => void = () => undefined;
    await setup({
      platform: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    expect(await screen.findByText("Loading the global rules")).toHaveAttribute(
      "role",
      "status",
    );
    answer(jsonResponse(200, readExample("PlatformView")));
    const stored = await screen.findByRole("table", {
      name: "The global rules, as stored",
    });
    expect(stored).toHaveTextContent("metadata and link-local");
    expect(
      screen.queryByText("Loading the global rules"),
    ).not.toBeInTheDocument();
  });

  it("offers a retry when the global rules cannot be read", async () => {
    const { user, calls } = await setup({
      platform: () =>
        jsonResponse(503, {
          error: { code: "unavailable", message: "Lakebase is down" },
        }),
    });
    expect(
      await screen.findByText(/Could not read the global rules/),
    ).toBeInTheDocument();
    const before = platformCalls(calls).length;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(platformCalls(calls).length).toBeGreaterThan(before);
  });
});
