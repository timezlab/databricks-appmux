import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EnvironmentPanel } from "@/components/app/environment/environment-panel";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

let answer: () => Response;

function route({ url }: RecordedCall): Response {
  if (url.endsWith("/api/v1/apps/sales/environment")) return answer();
  return jsonResponse(404, {
    error: { code: "not_found", message: "no such route in this test" },
  });
}

function setup(body: () => Response) {
  answer = body;
  stubFetch(route);
  const user = userEvent.setup();
  render(<EnvironmentPanel slug="sales" />);
  return user;
}

const example = () => jsonResponse(200, readExample("AppEnvironment"));

afterEach(() => {
  vi.restoreAllMocks();
});

describe("EnvironmentPanel", () => {
  it("shows a loading status, then the runtime and the variables", async () => {
    setup(example);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading the environment",
    );
    expect(await screen.findByText("3.11.9")).toBeInTheDocument();
    expect(screen.getByText("22.22.1")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Variables" });
    expect(within(table).getByText("LOG_LEVEL")).toBeInTheDocument();
    expect(within(table).getByText("resource key db-password")).toBeVisible();
  });

  it("shows a secret as a mask with a secret mark", async () => {
    setup(example);
    const row = (await screen.findByText("DB_PASSWORD")).closest("tr");
    expect(row).not.toBeNull();
    if (row === null) return;
    expect(within(row).getByText("***")).toBeInTheDocument();
    expect(within(row).getByText("secret")).toBeInTheDocument();
  });

  it("finds a variable by its name", async () => {
    const user = setup(example);
    await user.type(
      await screen.findByLabelText("Search variables"),
      "base_path",
    );
    const table = screen.getByRole("table", { name: "Variables" });
    expect(within(table).getAllByRole("row")).toHaveLength(2); // header + one
    expect(within(table).getByText("APPMUX_BASE_PATH")).toBeInTheDocument();
  });

  it("lists the packages on their tab, filtered by source", async () => {
    const user = setup(example);
    await user.click(await screen.findByRole("tab", { name: "Packages (3)" }));
    const table = screen.getByRole("table", { name: "Packages" });
    expect(within(table).getByText("==0.115.0")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Wheel" }));
    expect(within(table).queryByText("fastapi")).not.toBeInTheDocument();
    expect(within(table).getByText("acme-tools")).toBeInTheDocument();
  });

  it("shows the same data as JSON, with no secret value in it", async () => {
    const user = setup(() =>
      jsonResponse(200, {
        ...(readExample("AppEnvironment") as object),
        variables: [
          {
            name: "API_TOKEN",
            value: null,
            source: "resource",
            resource_key: "api-token",
            is_secret: true,
          },
        ],
      }),
    );
    await user.click(await screen.findByRole("radio", { name: "JSON" }));
    const json = screen.getByLabelText("Environment as JSON");
    expect(json.textContent).toContain('"name": "API_TOKEN"');
    expect(json.textContent).toContain('"value": null');
  });

  it("says there is no release yet and where to make one", async () => {
    setup(() =>
      jsonResponse(200, {
        release: null,
        runtime: { kind: "unknown", python_version: null, node_version: null },
        variables: [],
        packages: [],
      }),
    );
    expect(await screen.findByText("No release yet")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Go to Deployments" }),
    ).toHaveAttribute("href", expect.stringContaining("/app/deployments"));
  });

  it("says the environment could not be loaded and offers to try again", async () => {
    setup(() =>
      jsonResponse(503, {
        error: { code: "unavailable", message: "try later" },
      }),
    );
    expect(
      await screen.findByText(/Could not load the environment/),
    ).toBeInTheDocument();
  });
});
