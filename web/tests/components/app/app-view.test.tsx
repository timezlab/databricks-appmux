import { render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppDeploymentsPage } from "@/components/app/pages/deployments-page";
import { AppOverviewPage } from "@/components/app/pages/overview-page";
import { AppView } from "@/components/app/app-view";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";
import { readExample } from "../../support/contract-examples";

// The path on screen, as `usePathname()` gives it (no base path).
let pathname = "/app/";
vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
}));

// The four polls of the page answer by route; `appAnswer` is the one under test.
let appAnswer: () => Response;
let appCalls = 0;

function route({ url }: RecordedCall): Response {
  if (url.endsWith("/api/v1/apps/sales")) {
    appCalls += 1;
    return appAnswer();
  }
  if (url.endsWith("/api/v1/me"))
    return jsonResponse(200, readExample("MeResponse"));
  if (url.endsWith("/api/v1/apps"))
    return jsonResponse(200, readExample("AppList"));
  if (url.endsWith("/api/v1/requests")) {
    return jsonResponse(200, readExample("RequestList"));
  }
  return jsonResponse(404, {
    error: { code: "not_found", message: "no such route in this test" },
  });
}

function errorBody(code: string, message: string) {
  return { error: { code, message } };
}

function setup(page: ReactElement = <AppOverviewPage />): void {
  appCalls = 0;
  stubFetch(route);
  render(<AppView slug="sales">{page}</AppView>);
}

afterEach(() => {
  vi.restoreAllMocks();
  pathname = "/app/";
});

describe("AppView", () => {
  it("shows the app's name, its sections and the page that was asked for, once the app answers", async () => {
    appAnswer = () => jsonResponse(200, readExample("AppDetail"));
    setup();
    expect(
      await screen.findByRole("heading", { level: 1, name: "Sales dashboard" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Status" }),
    ).toBeInTheDocument();
    const sidebar = screen.getByRole("navigation", {
      name: "Sales dashboard sections",
    });
    // next/link adds the export's trailing slash only inside a Next build.
    expect(
      within(sidebar)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")?.replace("/?", "?")),
    ).toEqual([
      "/",
      "/app?slug=sales",
      "/app/authorization?slug=sales",
      "/app/deployments?slug=sales",
      "/app/logs?slug=sales",
      "/app/security?slug=sales",
      "/app/environment?slug=sales",
      "/app/resources?slug=sales",
      "/app/insights?slug=sales",
      "/app/requests?slug=sales",
      "/app/settings?slug=sales",
    ]);
    expect(
      within(sidebar).getByRole("link", { name: "All apps" }),
    ).toHaveAttribute("href", "/");
  });

  it("marks the section on screen as the current one, with or without the trailing slash", async () => {
    appAnswer = () => jsonResponse(200, readExample("AppDetail"));
    pathname = "/app/deployments";
    setup(<AppDeploymentsPage />);
    const sidebar = await screen.findByRole("navigation", {
      name: "Sales dashboard sections",
    });
    expect(
      within(sidebar)
        .getAllByRole("link", { current: "page" })
        .map((link) => link.textContent),
    ).toEqual(["Deployments"]);
  });

  it("puts only the page that was asked for on screen, so the other areas wait in their own sections", async () => {
    appAnswer = () => jsonResponse(200, readExample("AppDetail"));
    pathname = "/app/deployments/";
    setup(<AppDeploymentsPage />);
    expect(
      await screen.findByRole("heading", { level: 2, name: "Releases" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { level: 2, name: "Status" }),
    ).not.toBeInTheDocument();
  });

  it("explains a 403 as a page for admins, without an error banner", async () => {
    appAnswer = () =>
      jsonResponse(403, errorBody("forbidden", "CAN_MANAGE is required"));
    setup();
    expect(
      await screen.findByText("You cannot manage this app"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says a 404 means there is no such app", async () => {
    appAnswer = () =>
      jsonResponse(404, errorBody("not_found", "no app 'sales'"));
    setup();
    expect(await screen.findByText("There is no such app")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("announces a 503 and lets the person try again until the app loads", async () => {
    appAnswer = () =>
      jsonResponse(
        503,
        errorBody("unavailable", "the control plane is starting"),
      );
    const user = userEvent.setup();
    setup();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the app");
    expect(alert).toHaveTextContent("appmux is not available right now");
    expect(
      screen.getByRole("heading", { level: 1, name: "sales" }),
    ).toBeInTheDocument();

    appAnswer = () => jsonResponse(200, readExample("AppDetail"));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Sales dashboard" }),
    ).toBeInTheDocument();
    expect(appCalls).toBe(2);
  });
});
