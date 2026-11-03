import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppView } from "@/components/app/app-view";
import { AppSettingsPage } from "@/components/app/pages/settings-page";
import { API_BASE_URL } from "@/lib/api";
import { carriedOutcome, carryOutcome } from "@/lib/app/settings";
import { RELOAD_NOTICE_TEXT } from "@/lib/app/writes";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";
import { describeCall } from "./form-test-harness";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/settings/",
  useRouter: () => router,
}));

type Answer = (call: RecordedCall) => Response | Promise<Response>;

interface Server {
  isPlatformAdmin?: boolean;
  app?: Record<string, unknown>;
  requests?: unknown;
  patch?: Answer;
  owner?: Answer;
  remove?: Answer;
}

function exampleApp(overrides: Record<string, unknown> = {}) {
  return {
    ...(readExample("AppDetail") as Record<string, unknown>),
    ...overrides,
  };
}

function errorBody(status: number, code: string, message: string): Response {
  return jsonResponse(status, { error: { code, message } });
}

async function answeredSlug(response: Response): Promise<unknown> {
  const body: unknown = await response.clone().json();
  return typeof body === "object" && body !== null && "slug" in body
    ? body.slug
    : null;
}

/**
 * The four polls answer from the examples; the writes answer as the test says,
 * and are recorded. As on the server, a PATCH that answers with another slug
 * moves the app: the old slug answers 404 from then on, and those reads are
 * recorded too.
 */
function serve(server: Server = {}): {
  writes: RecordedCall[];
  readsOfOldSlug: RecordedCall[];
} {
  const writes: RecordedCall[] = [];
  const readsOfOldSlug: RecordedCall[] = [];
  let isMoved = false;
  stubFetch(async (call) => {
    const method = call.init.method ?? "GET";
    const path = call.url.replace(API_BASE_URL, "");
    if (method !== "GET") writes.push(call);
    if (method === "PATCH" && path === "/apps/sales") {
      const response = await (
        server.patch ?? (() => jsonResponse(200, exampleApp()))
      )(call);
      if (response.ok && (await answeredSlug(response)) !== "sales") {
        isMoved = true;
      }
      return response;
    }
    if (method === "PUT" && path === "/apps/sales/owner")
      return (server.owner ?? (() => jsonResponse(200, exampleApp())))(call);
    if (method === "PUT" && path === "/apps/revenue/owner")
      return (server.owner ?? (() => jsonResponse(200, exampleApp())))(call);
    if (method === "DELETE" && path === "/apps/sales")
      return (server.remove ?? (() => new Response(null, { status: 204 })))(
        call,
      );
    if (path === "/apps/sales" && isMoved) {
      readsOfOldSlug.push(call);
      return errorBody(404, "not_found", "app 'sales' not found");
    }
    if (path === "/apps/sales")
      return jsonResponse(200, exampleApp(server.app));
    if (path === "/apps/revenue")
      return jsonResponse(200, exampleApp({ ...server.app, slug: "revenue" }));
    if (path === "/me")
      return jsonResponse(200, {
        ...(readExample("MeResponse") as Record<string, unknown>),
        is_platform_admin: server.isPlatformAdmin ?? true,
      });
    if (path === "/apps") return jsonResponse(200, readExample("AppList"));
    if (path === "/requests")
      return jsonResponse(200, server.requests ?? readExample("RequestList"));
    return errorBody(404, "not_found", "no such route in this test");
  });
  return { writes, readsOfOldSlug };
}

/** The settings page of one slug, keyed by it as the app's route renders it. */
function settingsPageOf(slug: string): ReactElement {
  return (
    <AppView key={slug} slug={slug}>
      <AppSettingsPage />
    </AppView>
  );
}

async function setup(server: Server = {}) {
  const { writes, readsOfOldSlug } = serve(server);
  const user = userEvent.setup();
  const { rerender } = render(settingsPageOf("sales"));
  await screen.findByRole("heading", { level: 2, name: "Settings" });
  /** From now on `router.replace` puts the page it goes to on screen, as the app router does. */
  function followTheRouter(): void {
    router.replace.mockImplementation((path: string) => {
      const slug = new URL(path, "https://appmux.test").searchParams.get(
        "slug",
      );
      if (slug !== null) rerender(settingsPageOf(slug));
    });
  }
  return { user, writes, readsOfOldSlug, followTheRouter };
}

/** An answer the test gives when it chooses, and the function that gives it. */
function heldAnswer(): {
  answer: Promise<Response>;
  give: (response: Response) => void;
} {
  let give: (response: Response) => void = () => undefined;
  const answer = new Promise<Response>((resolve) => {
    give = resolve;
  });
  return { answer, give };
}

/** Types a new slug and a new owner, presses Save and confirms the move. */
async function renameAndChangeOwner(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  const form = await settingsForm();
  const slug = form.getByRole("textbox", { name: "Slug" });
  await user.clear(slug);
  await user.type(slug, "revenue");
  const owner = form.getByRole("textbox", { name: "Owner" });
  await user.clear(owner);
  await user.type(owner, "bob@example.com");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await user.click(
    screen.getByRole("button", { name: "Yes, move to /revenue/" }),
  );
}

const RENAME_AND_OWNER_WRITES = [
  {
    method: "PATCH",
    path: "/apps/sales",
    body: { version: 3, slug: "revenue" },
  },
  {
    method: "PUT",
    path: "/apps/revenue/owner",
    body: { version: 4, owner: "bob@example.com" },
  },
];

/** The platform admin's form, once `/me` has said who is looking. */
async function settingsForm() {
  return within(await screen.findByRole("form", { name: "Settings" }));
}

afterEach(() => {
  vi.restoreAllMocks();
  router.replace.mockReset();
  router.push.mockReset();
});

describe("AppSettingsPage, for a platform admin", () => {
  it("starts from the app's values and shows the save bar only once a field differs", async () => {
    const { user } = await setup();
    const form = await settingsForm();
    expect(form.getByRole("textbox", { name: "Name" })).toHaveValue(
      "Sales dashboard",
    );
    expect(form.getByRole("textbox", { name: "Slug" })).toHaveValue("sales");
    expect(form.getByRole("textbox", { name: "Owner" })).toHaveValue(
      "alice@example.com",
    );
    expect(
      screen.queryByRole("region", { name: "Unsaved changes" }),
    ).not.toBeInTheDocument();

    await user.type(form.getByRole("textbox", { name: "Name" }), "!");
    const bar = within(screen.getByRole("region", { name: "Unsaved changes" }));
    await user.click(bar.getByRole("button", { name: "Discard" }));
    expect(form.getByRole("textbox", { name: "Name" })).toHaveValue(
      "Sales dashboard",
    );
  });

  it("sends only the changed fields with the app's version, and says so", async () => {
    const { user, writes } = await setup();
    const form = await settingsForm();
    const memory = form.getByRole("textbox", { name: "Memory budget (MB)" });
    await user.clear(memory);
    await user.type(memory, "1024");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    // The shell shows it and says it in its live region.
    expect(
      await screen.findAllByText("Saved the settings of sales"),
    ).not.toHaveLength(0);
    expect(writes.map(describeCall)).toEqual([
      {
        method: "PATCH",
        path: "/apps/sales",
        body: { version: 3, memory_budget_mb: 1024 },
      },
    ]);
  });

  it.each([
    ["32", /must be at least 64/],
    ["lots", /must be a whole number of MB/],
    ["", /is required/],
  ])(
    "refuses a memory budget of %j, says why and moves the keyboard to it",
    async (typed, message) => {
      const { user, writes } = await setup();
      const form = await settingsForm();
      const memory = form.getByRole("textbox", { name: "Memory budget (MB)" });
      await user.clear(memory);
      if (typed !== "") await user.type(memory, typed);
      await user.click(screen.getByRole("button", { name: "Save changes" }));

      expect(memory).toBeInvalid();
      expect(memory).toHaveAccessibleDescription(message);
      expect(memory).toHaveFocus();
      expect(writes).toEqual([]);
    },
  );

  it("asks before a new slug moves the address, and Keep sends nothing", async () => {
    const { user, writes } = await setup();
    const form = await settingsForm();
    const slug = form.getByRole("textbox", { name: "Slug" });
    await user.clear(slug);
    await user.type(slug, "revenue");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const bar = within(screen.getByRole("region", { name: "Unsaved changes" }));
    expect(bar.getByText("Move sales to /revenue/?")).toBeInTheDocument();
    expect(bar.getByText(/old address answers 404/)).toBeInTheDocument();
    const keep = bar.getByRole("button", { name: "Keep /sales/" });
    expect(keep).toHaveFocus();
    await user.click(keep);
    expect(bar.getByRole("button", { name: "Save changes" })).toHaveFocus();
    expect(writes).toEqual([]);
  });

  it("moves the app and this page to the new slug once confirmed, carrying the outcome", async () => {
    const { user, writes } = await setup({
      patch: () => jsonResponse(200, exampleApp({ slug: "revenue" })),
    });
    const form = await settingsForm();
    const slug = form.getByRole("textbox", { name: "Slug" });
    await user.clear(slug);
    await user.type(slug, "revenue");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(
      screen.getByRole("button", { name: "Yes, move to /revenue/" }),
    );

    await vi.waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith(
        "/app/settings/?slug=revenue",
      );
    });
    expect(writes.map(describeCall)).toEqual([
      {
        method: "PATCH",
        path: "/apps/sales",
        body: { version: 3, slug: "revenue" },
      },
    ]);
    const outcome = carriedOutcome("/app/settings/?slug=revenue");
    expect(outcome?.title).toBe("Renamed sales to revenue");
    expect(outcome?.detail).toMatch(/URL prefix is saved as \/revenue\//);
  });

  it("moves to the new slug once the owner change has answered, and shows there that the owner was not changed", async () => {
    const owner = heldAnswer();
    const { user, writes } = await setup({
      patch: () =>
        jsonResponse(200, exampleApp({ slug: "revenue", version: 4 })),
      owner: () => owner.answer,
    });
    await renameAndChangeOwner(user);

    await vi.waitFor(() => {
      expect(writes.map(describeCall)).toEqual(RENAME_AND_OWNER_WRITES);
    });
    // The page stays where its one write is held until the owner change answers.
    expect(router.replace).not.toHaveBeenCalled();
    owner.give(
      errorBody(400, "bad_request", "owner must be a member of the workspace"),
    );
    await vi.waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith(
        "/app/settings/?slug=revenue",
      );
    });
    expect(router.replace).toHaveBeenCalledTimes(1);

    // The router lands on the new slug's page, which shows what both writes did.
    cleanup();
    render(settingsPageOf("revenue"));
    expect(
      await screen.findAllByText("Renamed sales to revenue"),
    ).not.toHaveLength(0);
    const alert = await screen.findByText(/the owner was not changed/);
    expect(alert.closest("[role=alert]")).not.toBeNull();
    expect(
      screen.getByText(/owner must be a member of the workspace/),
    ).toBeInTheDocument();
  });

  it("moves once both the rename and the owner change went through, with one notice that names the new owner", async () => {
    const owner = heldAnswer();
    const { user, writes, followTheRouter } = await setup({
      patch: () =>
        jsonResponse(200, exampleApp({ slug: "revenue", version: 4 })),
      owner: () => owner.answer,
    });
    followTheRouter();
    await renameAndChangeOwner(user);

    await vi.waitFor(() => {
      expect(writes.map(describeCall)).toEqual(RENAME_AND_OWNER_WRITES);
    });
    expect(router.replace).not.toHaveBeenCalled();
    owner.give(
      jsonResponse(
        200,
        exampleApp({ slug: "revenue", version: 5, owner: "bob@example.com" }),
      ),
    );

    const title = "Renamed sales to revenue; the owner is now bob@example.com";
    expect(await screen.findAllByText(title)).not.toHaveLength(0);
    expect(router.replace).toHaveBeenCalledTimes(1);
    expect(router.replace).toHaveBeenCalledWith("/app/settings/?slug=revenue");
    expect(
      screen.queryByText(/the owner was not changed/),
    ).not.toBeInTheDocument();
  });

  it("holds every other write while the owner change that follows a rename runs", async () => {
    const owner = heldAnswer();
    const { user, writes, followTheRouter } = await setup({
      patch: () =>
        jsonResponse(200, exampleApp({ slug: "revenue", version: 4 })),
      owner: () => owner.answer,
    });
    followTheRouter();
    await renameAndChangeOwner(user);
    await vi.waitFor(() => {
      expect(writes.map(describeCall)).toEqual(RENAME_AND_OWNER_WRITES);
    });

    // Whichever slug's page is on screen now, its Delete must wait.
    const open = await screen.findByRole("button", {
      name: /^Delete [a-z0-9-]+\.\.\.$/,
    });
    const shownSlug = (open.textContent ?? "").replace(/^Delete |\.\.\.$/g, "");
    await user.click(open);
    const confirm = within(
      screen.getByRole("form", { name: `Delete ${shownSlug}` }),
    );
    await user.type(
      confirm.getByRole("textbox", { name: `Type ${shownSlug} to delete it` }),
      shownSlug,
    );
    const remove = confirm.getByRole("button", { name: `Delete ${shownSlug}` });
    expect(remove).toBeDisabled();
    await user.click(remove);
    expect(writes.map(describeCall)).toEqual(RENAME_AND_OWNER_WRITES);
    owner.give(jsonResponse(200, exampleApp({ slug: "revenue", version: 5 })));
    await vi.waitFor(() => {
      expect(router.replace).toHaveBeenCalledTimes(1);
    });
  });

  it("keeps the app on screen when the old slug answers 404 before the page has moved", async () => {
    const { user, readsOfOldSlug } = await setup({
      patch: () => jsonResponse(200, exampleApp({ slug: "revenue" })),
    });
    const form = await settingsForm();
    const slug = form.getByRole("textbox", { name: "Slug" });
    await user.clear(slug);
    await user.type(slug, "revenue");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(
      screen.getByRole("button", { name: "Yes, move to /revenue/" }),
    );

    // The router here never takes the page away: the read after the write meets the 404.
    await vi.waitFor(() => {
      expect(readsOfOldSlug).not.toHaveLength(0);
    });
    await vi.waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Save changes" }),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("App not found")).not.toBeInTheDocument();
    expect(screen.queryByText("There is no such app")).not.toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Settings" })).toBeInTheDocument();
  });

  it("changes the owner alone with PUT, and both with the version the PATCH answered", async () => {
    const { user, writes } = await setup({
      patch: () => jsonResponse(200, exampleApp({ version: 4 })),
    });
    const form = await settingsForm();
    const owner = form.getByRole("textbox", { name: "Owner" });
    await user.clear(owner);
    await user.type(owner, "bob@example.com");
    await user.type(form.getByRole("textbox", { name: "Name" }), " EU");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findAllByText(
        "Saved the settings of sales; the owner is now bob@example.com",
      ),
    ).not.toHaveLength(0);
    expect(writes.map(describeCall)).toEqual([
      {
        method: "PATCH",
        path: "/apps/sales",
        body: { version: 3, display_name: "Sales dashboard EU" },
      },
      {
        method: "PUT",
        path: "/apps/sales/owner",
        body: { version: 4, owner: "bob@example.com" },
      },
    ]);
  });

  it("refuses an owner that is not a lower-case email before any call", async () => {
    const { user, writes } = await setup();
    const form = await settingsForm();
    const owner = form.getByRole("textbox", { name: "Owner" });
    await user.clear(owner);
    await user.type(owner, "Bob@Example.com");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(owner).toBeInvalid();
    expect(owner).toHaveAccessibleDescription(/must be lower case/);
    expect(writes).toEqual([]);
  });

  it("says which part went through when the owner fails after the fields were saved", async () => {
    const { user } = await setup({
      owner: () =>
        errorBody(409, "changed_by_others", "changed by someone else"),
    });
    const form = await settingsForm();
    await user.type(form.getByRole("textbox", { name: "Name" }), " EU");
    const owner = form.getByRole("textbox", { name: "Owner" });
    await user.clear(owner);
    await user.type(owner, "bob@example.com");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const alert = await screen.findByText(
      /The fields were saved; the owner was not changed/,
    );
    expect(alert.closest("[role=alert]")).not.toBeNull();
    expect(
      within(alert.closest("[role=alert]") ?? alert).getByRole("button", {
        name: "Reload",
      }),
    ).toBeInTheDocument();
  });

  it("shows the server's refusal of a taken slug, offers no reload, and keeps what was typed", async () => {
    const { user } = await setup({
      patch: () =>
        errorBody(409, "slug_taken", "slug 'reports' is already taken"),
    });
    const form = await settingsForm();
    const slug = form.getByRole("textbox", { name: "Slug" });
    await user.clear(slug);
    await user.type(slug, "reports");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(
      screen.getByRole("button", { name: "Yes, move to /reports/" }),
    );

    const reason = await screen.findByText(/slug 'reports' is already taken/);
    const alert = reason.closest<HTMLElement>("[role=alert]");
    expect(alert).not.toBeNull();
    expect(alert).toHaveTextContent(
      "The settings were not saved: Not possible in the current state",
    );
    // A reload cannot free the slug: the way out is another slug.
    expect(alert).not.toHaveTextContent(/Reload/);
    expect(
      within(alert ?? reason).queryByRole("button"),
    ).not.toBeInTheDocument();
    expect(slug).toHaveValue("reports");
    expect(router.replace).not.toHaveBeenCalled();
    expect(screen.queryByText(RELOAD_NOTICE_TEXT)).not.toBeInTheDocument();
    expect(
      screen.queryByText("Changed by someone else"),
    ).not.toBeInTheDocument();
  });

  it("warns that a Node.js app needs 768 MB and fills it on request", async () => {
    const { user } = await setup({ app: { runtime: "node" } });
    const form = await settingsForm();
    expect(
      form.getByText(/Node\.js needs about 768 MB of memory; with 512 MB/),
    ).toBeInTheDocument();
    await user.click(form.getByRole("button", { name: "Use 768 MB" }));
    expect(
      form.getByRole("textbox", { name: "Memory budget (MB)" }),
    ).toHaveValue("768");
    expect(
      form.queryByText(/Node\.js needs about 768 MB/),
    ).not.toBeInTheDocument();
  });

  it("deletes the app only once its slug is typed, then goes to the platform's list of apps", async () => {
    const { user, writes } = await setup();
    await user.click(
      await screen.findByRole("button", { name: "Delete sales..." }),
    );
    const confirm = within(screen.getByRole("form", { name: "Delete sales" }));
    const field = confirm.getByRole("textbox", {
      name: "Type sales to delete it",
    });
    expect(field).toHaveFocus();
    const remove = confirm.getByRole("button", { name: "Delete sales" });
    expect(remove).toBeDisabled();
    expect(
      confirm.getByText("Type the slug above to turn on Delete."),
    ).toBeInTheDocument();

    await user.type(field, "sale");
    expect(remove).toBeDisabled();
    await user.type(field, "s");
    await user.click(remove);

    await vi.waitFor(() => {
      expect(router.push).toHaveBeenCalledWith("/platform/apps/");
    });
    expect(writes.map(describeCall)).toEqual([
      { method: "DELETE", path: "/apps/sales", body: undefined },
    ]);
    expect(carriedOutcome("/platform/apps/")?.title).toBe("Deleted sales");
  });

  it("puts the delete question away on Escape and hands the keyboard back", async () => {
    const { user, writes } = await setup();
    const open = await screen.findByRole("button", { name: "Delete sales..." });
    await user.click(open);
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("button", { name: "Delete sales..." }),
    ).toHaveFocus();
    expect(writes).toEqual([]);
  });
});

describe("AppSettingsPage, for an app admin", () => {
  const noRequests = { items: [] };

  it("shows the values read-only, with why the owner and delete are not theirs", async () => {
    await setup({ isPlatformAdmin: false, requests: noRequests });
    expect(
      await screen.findByText("Only a platform admin changes the owner."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Only a platform admin deletes an app."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("form", { name: "Settings" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      screen.getByText("/Workspace/Shared/apps/sales"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Delete/ }),
    ).not.toBeInTheDocument();
  });

  it("opens the request form in place from Request a change", async () => {
    const { user } = await setup({
      isPlatformAdmin: false,
      requests: noRequests,
    });
    const ask = await screen.findByRole("button", {
      name: "Request a change",
    });
    await vi.waitFor(() => {
      expect(ask).toBeEnabled();
    });
    await user.click(ask);
    expect(ask).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("form", { name: "Request a change of settings" }),
    ).toBeInTheDocument();
  });

  it("keeps Request a change off, with the reason beside it, while one waits", async () => {
    await setup({ isPlatformAdmin: false });
    const ask = await screen.findByRole("button", {
      name: "Request a change",
    });
    expect(
      await screen.findByText(/A request of this kind is waiting/),
    ).toBeInTheDocument();
    expect(ask).toBeDisabled();
  });
});

describe("AppSettingsPage, after a rename", () => {
  it("shows and announces the outcome the old slug's page carried, once", async () => {
    carryOutcome("/app/settings/?slug=sales", {
      title: "Renamed shop to sales",
      detail: "The app's URL prefix is saved as /sales/.",
    });
    await setup();
    expect(screen.getAllByText("Renamed shop to sales").length).toBeGreaterThan(
      0,
    );
    expect(carriedOutcome("/app/settings/?slug=sales")).toBeNull();
  });
});
