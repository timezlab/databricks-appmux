import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppSwitcher } from "@/components/app-switcher";
import { readExample } from "../support/contract-examples";
import { jsonResponse, stubFetch } from "../support/fake-fetch";
import type { RecordedCall } from "../support/fake-fetch";

const navigation = vi.hoisted(() => ({
  pathname: "/app/requests/",
  push: vi.fn<(href: string) => void>(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useRouter: () => ({ push: navigation.push }),
}));

// sales "Sales dashboard" (running), docs "Team docs" (stopped), reports "Weekly reports" (running).
const APP_LIST = readExample("AppList");

let calls: RecordedCall[] = [];
let respond: () => Response | Promise<Response> = () =>
  jsonResponse(200, APP_LIST);

beforeEach(() => {
  calls = [];
  respond = () => jsonResponse(200, APP_LIST);
  navigation.pathname = "/app/requests/";
  navigation.push.mockReset();
  stubFetch((call) => {
    calls.push(call);
    return respond();
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function trigger(name: RegExp | string = /^App: /): HTMLElement {
  return screen.getByRole("button", { name });
}

async function openSwitcher(user: ReturnType<typeof userEvent.setup>) {
  await user.click(trigger());
  return screen.findByRole("dialog", { name: "Switch app" });
}

describe("AppSwitcher", () => {
  it("shows the app's route strip while its name is unknown, and asks for nothing until it is opened", async () => {
    render(<AppSwitcher slug="unlisted" />);
    expect(trigger("App: /unlisted/")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // Give a stray effect time to fire a request.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(calls).toHaveLength(0);
  });

  it("loads the apps when it opens, showing a skeleton until they arrive", async () => {
    const user = userEvent.setup();
    let release: () => void = () => undefined;
    respond = () =>
      new Promise((resolve) => {
        release = () => {
          resolve(jsonResponse(200, APP_LIST));
        };
      });
    render(<AppSwitcher slug="sales" />);

    const dialog = await openSwitcher(user);
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    expect(calls.map((call) => call.url)).toEqual(["/_appmux/api/v1/apps"]);
    expect(dialog.querySelector('[data-skeleton="lines"]')).not.toBeNull();
    expect(within(dialog).getByRole("status")).toHaveTextContent(
      "Loading the apps",
    );

    release();
    const rows = await within(dialog).findAllByRole("option");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Sales dashboard");
    expect(rows[0]).toHaveTextContent("/sales/");
    expect(rows[0]).toHaveTextContent("Running");
    expect(rows[1]).toHaveTextContent("Team docs");
    expect(rows[1]).toHaveTextContent("Stopped");
    expect(dialog.querySelector('[data-skeleton="lines"]')).toBeNull();
  });

  it("filters by name or slug as the person types", async () => {
    const user = userEvent.setup();
    render(<AppSwitcher slug="sales" />);
    const dialog = await openSwitcher(user);
    await within(dialog).findAllByRole("option");

    await user.type(within(dialog).getByRole("combobox"), "weekly");
    expect(within(dialog).getAllByRole("option")).toHaveLength(1);
    expect(within(dialog).getByRole("option")).toHaveTextContent(
      "Weekly reports",
    );

    await user.clear(within(dialog).getByRole("combobox"));
    await user.type(within(dialog).getByRole("combobox"), "docs");
    expect(within(dialog).getByRole("option")).toHaveTextContent("Team docs");

    await user.clear(within(dialog).getByRole("combobox"));
    await user.type(within(dialog).getByRole("combobox"), "nothing-like-it");
    expect(within(dialog).queryAllByRole("option")).toHaveLength(0);
    expect(dialog).toHaveTextContent("No app matches");
  });

  it("goes to the same section of the chosen app and closes", async () => {
    const user = userEvent.setup();
    render(<AppSwitcher slug="sales" />);
    const dialog = await openSwitcher(user);

    await user.click(
      await within(dialog).findByRole("option", { name: /Team docs/ }),
    );
    expect(navigation.push).toHaveBeenCalledWith("/app/requests/?slug=docs");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
  });

  it("goes to the chosen app's overview from a page that has no twin in another app", async () => {
    const user = userEvent.setup();
    navigation.pathname = "/app/not-a-section/";
    render(<AppSwitcher slug="sales" />);
    const dialog = await openSwitcher(user);

    await user.click(
      await within(dialog).findByRole("option", { name: /Weekly reports/ }),
    );
    expect(navigation.push).toHaveBeenCalledWith("/app/?slug=reports");
  });

  it("chooses with the keyboard, and marks the app on screen", async () => {
    const user = userEvent.setup();
    navigation.pathname = "/app/";
    render(<AppSwitcher slug="sales" />);
    const dialog = await openSwitcher(user);
    const current = await within(dialog).findByRole("option", {
      name: /Sales dashboard/,
    });
    expect(current).toHaveTextContent("(current)");

    await user.keyboard("{ArrowDown}{Enter}");
    expect(navigation.push).toHaveBeenCalledWith("/app/?slug=docs");
  });

  it("closes on Escape without going anywhere", async () => {
    const user = userEvent.setup();
    render(<AppSwitcher slug="sales" />);
    const dialog = await openSwitcher(user);
    await within(dialog).findAllByRole("option");

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("says the list could not load and loads it again on Try again", async () => {
    const user = userEvent.setup();
    respond = () =>
      jsonResponse(503, {
        error: { code: "not_ready", message: "appmux is starting" },
      });
    render(<AppSwitcher slug="sales" />);
    const dialog = await openSwitcher(user);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Could not load the apps",
    );
    respond = () => jsonResponse(200, APP_LIST);
    await user.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(await within(dialog).findAllByRole("option")).toHaveLength(3);
  });

  it("says there are no apps and links to the launcher when the list is empty", async () => {
    const user = userEvent.setup();
    respond = () => jsonResponse(200, { items: [] });
    render(<AppSwitcher slug="sales" />);
    const dialog = await openSwitcher(user);

    expect(await within(dialog).findByText("No apps yet")).toBeInTheDocument();
    expect(
      within(dialog).getByRole("link", { name: "Go to the launcher" }),
    ).toHaveAttribute("href", "/");
  });

  it("names the app by its display name once the list has been read", async () => {
    const user = userEvent.setup();
    render(<AppSwitcher slug="reports" />);
    const dialog = await openSwitcher(user);
    await within(dialog).findAllByRole("option");
    expect(trigger("App: Weekly reports")).toBeInTheDocument();
  });

  it("renders on the server with no style attribute", () => {
    const html = renderToStaticMarkup(<AppSwitcher slug="sales" />);
    expect(html).not.toMatch(/\sstyle=/);
    expect(html).toContain("/sales/");
  });
});
