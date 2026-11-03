import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddResourcePanel } from "@/components/app/resources/add-resource-panel";
import { ResourceTypeList } from "@/lib/api";
import type {
  AppDetail,
  ResourceSuggestionList,
  ResourceTypeEntry,
} from "@/lib/api";
import { emptyAttachDraft } from "@/lib/app/resources";
import type { ResourceCatalogue } from "@/lib/app/resources";
import { stubFetch } from "../../../support/fake-fetch";
import type { RecordedCall } from "../../../support/fake-fetch";
import {
  APP,
  describeCall,
  jsonResponse,
  readExample,
  renderWithActions,
} from "../form-test-harness";

afterEach(() => {
  vi.restoreAllMocks();
});

const EXAMPLE_TYPES = ResourceTypeList.parse(
  readExample("ResourceTypeList"),
).items;

const UC_TABLE: ResourceTypeEntry = {
  type: "uc_table",
  label: "Unity Catalog table",
  description: "A table the app reads or writes.",
  permissions: ["SELECT", "MODIFY"],
  access_as: ["obo", "own_sp"],
  target_hint: "main.sales.orders",
  can_suggest: false,
};

const CATALOGUE: ResourceCatalogue = [...EXAMPLE_TYPES, UC_TABLE];

const SUGGESTED = readExample("ResourceSuggestionList");

/** Suggestions answer `suggest(url)`; every other call is the attach, answered by `attach()`. */
function stubCalls({
  suggest = () => jsonResponse(200, SUGGESTED),
  attach = () => jsonResponse(200, readExample("ResourceView")),
}: {
  suggest?: (url: URL) => Response;
  attach?: () => Response;
} = {}) {
  const suggestions: RecordedCall[] = [];
  const attaches: RecordedCall[] = [];
  stubFetch((call) => {
    if (call.url.includes("/resources/suggestions")) {
      suggestions.push(call);
      return suggest(new URL(call.url, "http://appmux.test"));
    }
    attaches.push(call);
    return attach();
  });
  return { suggestions, attaches };
}

function renderPanel(app: AppDetail = APP) {
  const onAdded = vi.fn();
  const onClose = vi.fn();
  const view = renderWithActions((actions) => (
    <AddResourcePanel
      app={app}
      catalogue={CATALOGUE}
      initialDraft={emptyAttachDraft()}
      actions={actions}
      onClose={onClose}
      onAdded={onAdded}
      onReload={vi.fn()}
    />
  ));
  return { ...view, onAdded, onClose, user: userEvent.setup() };
}

type User = ReturnType<typeof userEvent.setup>;

async function chooseType(user: User, name: RegExp) {
  await user.click(screen.getByRole("combobox", { name: /^Type/ }));
  await user.click(screen.getByRole("option", { name }));
}

async function chooseAccessMode(user: User, name: RegExp) {
  await user.click(screen.getByRole("combobox", { name: /^Access mode/ }));
  await user.click(screen.getByRole("option", { name }));
}

function keyField() {
  return screen.getByRole("textbox", { name: /^Key/ });
}

describe("AddResourcePanel", () => {
  it("opens with the keyboard on its first field, the type, listed from the server's catalogue", async () => {
    stubCalls();
    const { user } = renderPanel();

    const type = screen.getByRole("combobox", { name: /^Type/ });
    expect(type).toHaveFocus();
    await user.click(type);
    expect(
      screen.getByRole("option", {
        name: /^SQL warehouse.*A SQL warehouse the app runs statements on/,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: /^Unity Catalog table/ }),
    ).toBeInTheDocument();
  });

  it("adds an object chosen from the suggestions, with the key filled from its name", async () => {
    const { suggestions, attaches } = stubCalls();
    const { user, onAdded, onChanged } = renderPanel();

    await chooseType(user, /^SQL warehouse/);
    const object = screen.getByRole("combobox", { name: /^SQL warehouse/ });
    await waitFor(() => {
      expect(object).toBeEnabled();
    });
    expect(describeCall(suggestions[0]).path).toBe(
      "/resources/suggestions?type=sql_warehouse",
    );

    await user.click(object);
    const shared = screen.getByRole("option", { name: "Shared Serverless" });
    expect(shared).toHaveTextContent("RUNNING, size Small");
    await user.click(shared);

    expect(object).toHaveTextContent("Shared Serverless");
    expect(object).toHaveTextContent("1234567890abcdef");
    expect(keyField()).toHaveValue("shared-serverless");
    expect(
      screen.getByRole("combobox", { name: /^Permission/ }),
    ).toHaveTextContent("Can use");

    await chooseAccessMode(user, /^As the visitor/);
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(onAdded).toHaveBeenCalledExactlyOnceWith("shared-serverless");
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(describeCall(attaches[0])).toEqual({
      method: "PUT",
      path: "/apps/sales/resources/shared-serverless",
      body: {
        type: "sql_warehouse",
        target: "1234567890abcdef",
        permission: "CAN_USE",
        access_as: "obo",
      },
    });
  });

  it("highlights the first object once the answer arrives, even when the text was typed before it", async () => {
    stubCalls({
      // Nothing to show before a name is typed; the search finds two.
      suggest: (url) =>
        url.searchParams.has("q")
          ? jsonResponse(200, SUGGESTED)
          : jsonResponse(200, {
              items: [],
              truncated: false,
              source: "caller_token",
              note: null,
            }),
    });
    const { user } = renderPanel();
    await chooseType(user, /^SQL warehouse/);
    const object = screen.getByRole("combobox", { name: /^SQL warehouse/ });
    await waitFor(() => {
      expect(object).toBeEnabled();
    });

    await user.click(object);
    await user.paste("re");
    const first = await screen.findByRole("option", { name: "Reporting" });
    await waitFor(() => {
      expect(first).toHaveAttribute("aria-selected", "true");
    });
    expect(screen.getByRole("option", { name: "Use “re”" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("turns off an access mode the app does not have, with the reason, and says how to ask for one", async () => {
    stubCalls();
    const { user } = renderPanel();
    await chooseType(user, /^SQL warehouse/);

    await user.click(screen.getByRole("combobox", { name: /^Access mode/ }));
    expect(
      screen.getByRole("option", { name: /^As the shared service principal/ }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(
      screen.getByRole("option", { name: /^As the visitor/ }),
    ).not.toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");

    expect(
      screen.getByText(
        /A platform admin has switched shared service principal delegation off/,
      ),
    ).toBeInTheDocument();
  });

  it("lets OBO be added before the platform delegates it, and says what the visitor's token needs", async () => {
    // Setting is not use: the resource is stored now; visitors' tokens reach the app after the delegation.
    const { attaches } = stubCalls();
    const { user, onAdded } = renderPanel({
      ...APP,
      delegation: { ...APP.delegation, obo_delegated: false },
    });
    await chooseType(user, /^SQL warehouse/);
    const object = screen.getByRole("combobox", { name: /^SQL warehouse/ });
    await waitFor(() => {
      expect(object).toBeEnabled();
    });
    await user.click(object);
    await user.click(screen.getByRole("option", { name: "Shared Serverless" }));

    await user.click(screen.getByRole("combobox", { name: /^Access mode/ }));
    expect(
      screen.getByRole("option", { name: /^As the visitor/ }),
    ).not.toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("option", { name: /^As the visitor/ }));

    expect(
      screen.getByText(/Until a platform admin delegates OBO to this app/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open Authorization" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => {
      expect(onAdded).toHaveBeenCalledExactlyOnceWith("shared-serverless");
    });
    expect(describeCall(attaches[0]).body).toMatchObject({ access_as: "obo" });
  });

  it("goes from a secret scope on to its keys, and fills the key from the secret's", async () => {
    const { attaches } = stubCalls({
      suggest: (url) => {
        const query = url.searchParams.get("q");
        const list: ResourceSuggestionList =
          query === "sales-secrets/"
            ? {
                items: [
                  {
                    target: "sales-secrets/db-password",
                    label: "db-password",
                    detail: null,
                    permissions: ["READ"],
                  },
                ],
                truncated: false,
                source: "caller_token",
                note: null,
              }
            : {
                items: [
                  {
                    target: "sales-secrets/",
                    label: "sales-secrets",
                    detail: "Secret scope",
                    permissions: ["READ"],
                  },
                ],
                truncated: false,
                source: "caller_token",
                note: null,
              };
        return jsonResponse(200, list);
      },
    });
    const { user, onAdded } = renderPanel();

    await chooseType(user, /^Secret/);
    expect(
      screen.getByText(/appmux reads a secret itself when the app starts/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: /^Access mode/ }),
    ).not.toBeInTheDocument();

    const object = screen.getByRole("combobox", { name: /^Secret/ });
    await waitFor(() => {
      expect(object).toBeEnabled();
    });
    await user.click(object);
    await user.click(
      screen.getByRole("option", { name: "sales-secrets, open its keys" }),
    );
    expect(screen.getByPlaceholderText(/Type a scope/)).toHaveValue(
      "sales-secrets/",
    );
    await user.click(
      await screen.findByRole("option", { name: "db-password" }),
    );

    expect(keyField()).toHaveValue("db-password");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => {
      expect(onAdded).toHaveBeenCalledExactlyOnceWith("db-password");
    });
    expect(describeCall(attaches[0]).body).toEqual({
      type: "secret",
      target: "sales-secrets/db-password",
      permission: "READ",
    });
  });

  it("turns into a text field with the server's note when Databricks gives no list", async () => {
    const { attaches } = stubCalls({
      suggest: () =>
        jsonResponse(200, {
          items: [],
          truncated: false,
          source: "unavailable",
          note: "Databricks did not list the warehouses just now; type the id.",
        }),
    });
    const { user, onAdded } = renderPanel();
    await chooseType(user, /^SQL warehouse/);

    const target = await screen.findByRole("textbox", {
      name: /^SQL warehouse/,
    });
    expect(target).toHaveAttribute("placeholder", "1234567890abcdef");
    expect(target).toHaveAccessibleDescription(
      /Databricks did not list the warehouses just now/,
    );

    await user.type(target, "fedcba0987654321");
    // The app already has a `sql-warehouse`: the key filled from the type is made unique.
    expect(keyField()).toHaveValue("sql-warehouse-2");
    await chooseAccessMode(user, /^As the app's own service principal/);
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(onAdded).toHaveBeenCalledExactlyOnceWith("sql-warehouse-2");
    });
    expect(describeCall(attaches[0]).body).toEqual({
      type: "sql_warehouse",
      target: "fedcba0987654321",
      permission: "CAN_USE",
      access_as: "own_sp",
    });
  });

  it("asks nothing of the server for a type that has no list, and gives its example", async () => {
    const { suggestions } = stubCalls();
    const { user } = renderPanel();
    await chooseType(user, /^Unity Catalog table/);

    const target = screen.getByRole("textbox", {
      name: /^Unity Catalog table/,
    });
    expect(target).toHaveAttribute("placeholder", "main.sales.orders");
    expect(target).toHaveAccessibleDescription(
      /There is no list of these to choose from; type it, such as main\.sales\.orders\./,
    );
    expect(suggestions).toHaveLength(0);
  });

  it("checks the form before sending: an announced error on the first field, nothing sent", async () => {
    const { attaches } = stubCalls();
    const { user } = renderPanel();

    await user.click(screen.getByRole("button", { name: "Add" }));

    const type = screen.getByRole("combobox", { name: /^Type/ });
    expect(type).toHaveAttribute("aria-invalid", "true");
    expect(type).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The resource was not attached",
    );
    expect(attaches).toHaveLength(0);
  });

  it("shows the server's refusal in words and keeps what was chosen", async () => {
    stubCalls({
      attach: () =>
        jsonResponse(403, {
          error: {
            code: "forbidden",
            message:
              "your token cannot reach SQL warehouse 1234567890abcdef (CAN_USE)",
          },
        }),
    });
    const { user, onAdded, onChanged } = renderPanel();

    await chooseType(user, /^SQL warehouse/);
    const object = screen.getByRole("combobox", { name: /^SQL warehouse/ });
    await waitFor(() => {
      expect(object).toBeEnabled();
    });
    await user.click(object);
    await user.click(screen.getByRole("option", { name: "Shared Serverless" }));
    await chooseAccessMode(user, /^As the visitor/);
    await user.click(screen.getByRole("button", { name: "Add" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The resource was not attached");
    expect(alert).toHaveTextContent(
      "your token cannot reach SQL warehouse 1234567890abcdef",
    );
    expect(onAdded).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
    const form = screen.getByRole("form", { name: "Add a resource" });
    expect(
      within(form).getByRole("combobox", { name: /^SQL warehouse/ }),
    ).toHaveTextContent("Shared Serverless");
    expect(keyField()).toHaveValue("shared-serverless");
  });
});
