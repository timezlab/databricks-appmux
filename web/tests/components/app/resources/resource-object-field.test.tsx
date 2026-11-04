import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResourceObjectField } from "@/components/app/resources/resource-object-field";
import type {
  ResourceSuggestion,
  ResourceSuggestionList,
  ResourceTypeEntry,
} from "@/lib/api";
import { jsonResponse, stubFetch } from "../../../support/fake-fetch";

afterEach(() => {
  vi.restoreAllMocks();
});

const PLATFORM_LINE =
  "Listed with appmux's own access, so it shows what appmux can read, not what you can.";

const UC_TABLE: ResourceTypeEntry = {
  type: "uc_table",
  label: "Unity Catalog table",
  description: "A table the app reads or writes.",
  permissions: ["SELECT", "MODIFY"],
  access_as: ["obo", "own_sp"],
  target_hint: "main.sales.orders",
  can_suggest: true,
};

const SECRET: ResourceTypeEntry = {
  type: "secret",
  label: "secret",
  description: "A secret appmux reads when the app starts.",
  permissions: ["READ"],
  access_as: [],
  target_hint: "my-scope/my-key",
  can_suggest: true,
};

function suggestion(
  target: string,
  label: string,
  detail: string | null = null,
): ResourceSuggestion {
  return { target, label, detail, permissions: ["SELECT"] };
}

function listOf(
  items: ResourceSuggestion[],
  overrides: Partial<ResourceSuggestionList> = {},
): ResourceSuggestionList {
  return {
    items,
    truncated: false,
    source: "caller_token",
    note: null,
    ...overrides,
  };
}

/** Answers each `q` with the list the test names; no `q` is the first look. */
function stubLists(
  answers: Record<string, ResourceSuggestionList>,
): (string | null)[] {
  const asked: (string | null)[] = [];
  stubFetch((call) => {
    const query = new URL(call.url, "http://appmux.test").searchParams.get("q");
    asked.push(query);
    return jsonResponse(200, answers[query ?? ""] ?? listOf([]));
  });
  return asked;
}

/** The field with the state the Add panel keeps for it. */
function Field({ entry }: { entry: ResourceTypeEntry }) {
  const [target, setTarget] = useState("");
  const [objectName, setObjectName] = useState<string | null>(null);
  return (
    <>
      <label htmlFor="object">Object</label>
      <ResourceObjectField
        id="object"
        entry={entry}
        target={target}
        objectName={objectName}
        onPick={(picked) => {
          setTarget(picked.target);
          setObjectName(picked.label);
        }}
        onType={(typed) => {
          setTarget(typed);
          setObjectName(null);
        }}
      />
    </>
  );
}

/** The search box in the open list. */
function searchBox() {
  return screen.getByRole("combobox", { name: /^Choose the/ });
}

async function openField(entry: ResourceTypeEntry) {
  const user = userEvent.setup();
  render(<Field entry={entry} />);
  const trigger = screen.getByRole("combobox", { name: "Object" });
  await waitFor(() => {
    expect(trigger).toBeEnabled();
  });
  await user.click(trigger);
  return { user, trigger };
}

describe("ResourceObjectField", () => {
  it("goes from the catalog to the schema to the table in one field", async () => {
    const asked = stubLists({
      "": listOf([suggestion("main.", "main", "catalog")]),
      "main.": listOf([suggestion("main.sales.", "sales", "schema")]),
      "main.sales.": listOf([
        suggestion("main.sales.orders", "orders", "MANAGED"),
      ]),
    });
    const { user, trigger } = await openField(UC_TABLE);

    await user.click(
      screen.getByRole("option", { name: "main, go inside it" }),
    );
    expect(searchBox()).toHaveValue("main.");
    // Going inside a level goes on typing: the keyboard stays in the search box.
    expect(searchBox()).toHaveFocus();
    await user.click(
      await screen.findByRole("option", { name: "sales, go inside it" }),
    );
    expect(searchBox()).toHaveValue("main.sales.");
    await user.click(await screen.findByRole("option", { name: "orders" }));

    expect(trigger).toHaveTextContent("orders");
    expect(trigger).toHaveTextContent("main.sales.orders");
    expect(asked).toEqual([null, "main.", "main.sales."]);
  });

  it("does not offer a step as the thing to use, but does offer the whole name", async () => {
    stubLists({
      "": listOf([suggestion("main.", "main", "catalog")]),
      "main.": listOf([suggestion("main.sales.", "sales", "schema")]),
    });
    const { user, trigger } = await openField(UC_TABLE);

    await user.paste("main.");
    await screen.findByRole("option", { name: "sales, go inside it" });
    expect(
      screen.queryByRole("option", { name: /^Use / }),
    ).not.toBeInTheDocument();

    await user.paste("sales.orders");
    const use = await screen.findByRole("option", {
      name: "Use “main.sales.orders”",
    });
    await user.click(use);

    expect(trigger).toHaveTextContent("main.sales.orders");
  });

  it("keeps the field open with the note when a deeper level cannot be listed", async () => {
    stubLists({
      "": listOf([suggestion("main.", "main", "catalog")]),
      "main.": listOf([], {
        source: "unavailable",
        note: "The schemas could not be listed just now; type the name in full.",
      }),
    });
    const { user, trigger } = await openField(UC_TABLE);

    await user.click(
      screen.getByRole("option", { name: "main, go inside it" }),
    );
    expect(
      await screen.findByText(/The schemas could not be listed just now/),
    ).toBeInTheDocument();

    await user.paste("sales.orders");
    await user.click(
      await screen.findByRole("option", { name: "Use “main.sales.orders”" }),
    );
    expect(trigger).toHaveTextContent("main.sales.orders");
  });

  it("lists from what appmux can read and says so when the answer came with its own access", async () => {
    stubLists({
      "": listOf([suggestion("sales-secrets/", "sales-secrets")], {
        source: "platform_token",
      }),
    });
    const { user } = await openField(SECRET);

    expect(
      screen.getByRole("option", { name: "sales-secrets, open its keys" }),
    ).toBeInTheDocument();
    expect(screen.getByText(PLATFORM_LINE)).toBeInTheDocument();
    await user.keyboard("{Escape}");
  });

  it("says nothing about appmux's access when the person's own token listed it", async () => {
    stubLists({
      "": listOf([suggestion("sales-secrets/", "sales-secrets")]),
    });
    await openField(SECRET);

    expect(
      screen.getByRole("option", { name: "sales-secrets, open its keys" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(PLATFORM_LINE)).not.toBeInTheDocument();
  });

  it("is a text field with the server's words when the type cannot be listed", async () => {
    stubLists({
      "": listOf([], {
        source: "not_supported",
        note: "Databricks has no list of these for an app to read.",
      }),
    });
    render(<Field entry={UC_TABLE} />);

    const target = await screen.findByRole("textbox");
    expect(target).toHaveAccessibleDescription(
      "Databricks has no list of these for an app to read.",
    );
  });
});
