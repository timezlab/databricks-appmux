/**
 * The source-folder field with its browser, against a stubbed
 * `GET /workspace/browse`: the roots, a folder, an app-looking folder, each
 * refusal of the route, and that the field stays free to type in.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Field } from "@/components/field";
import {
  BROWSE_PATH_HINT,
  SourceFolderInput,
  TYPE_PATH_HINT,
} from "@/components/folder-browser";
import type { WorkspaceListing } from "@/lib/api";
import { jsonResponse, stubFetch } from "../support/fake-fetch";
import type { RecordedCall } from "../support/fake-fetch";

const MY_FOLDER = "/Workspace/Users/alice@example.com";

const ROOTS: WorkspaceListing = {
  path: null,
  parent: null,
  roots: [
    { label: "My folder", path: MY_FOLDER },
    { label: "Shared", path: "/Workspace/Shared" },
  ],
  entries: [],
  truncated: false,
  markers: [],
  looks_like_app: false,
  runtime_hint: null,
  suggested_memory_mb: null,
};

function folder(path: string, overrides: Partial<WorkspaceListing> = {}) {
  return {
    path,
    parent: null,
    roots: [],
    entries: [],
    truncated: false,
    markers: [],
    looks_like_app: false,
    runtime_hint: null,
    suggested_memory_mb: null,
    ...overrides,
  } satisfies WorkspaceListing;
}

const LISTINGS = new Map<string, WorkspaceListing>([
  [
    MY_FOLDER,
    folder(MY_FOLDER, {
      entries: [
        { name: "notes.txt", path: `${MY_FOLDER}/notes.txt`, kind: "file" },
        {
          name: "sales-board",
          path: `${MY_FOLDER}/sales-board`,
          kind: "folder",
        },
        { name: "archive", path: `${MY_FOLDER}/archive`, kind: "folder" },
      ],
    }),
  ],
  [
    `${MY_FOLDER}/sales-board`,
    folder(`${MY_FOLDER}/sales-board`, {
      parent: MY_FOLDER,
      entries: [
        {
          name: "server",
          path: `${MY_FOLDER}/sales-board/server`,
          kind: "folder",
        },
        {
          name: "package.json",
          path: `${MY_FOLDER}/sales-board/package.json`,
          kind: "file",
        },
      ],
      markers: ["app.yaml", "package.json"],
      looks_like_app: true,
      runtime_hint: "node",
      suggested_memory_mb: 768,
    }),
  ],
  [
    `${MY_FOLDER}/archive`,
    folder(`${MY_FOLDER}/archive`, { parent: MY_FOLDER }),
  ],
]);

/** Paths that the stub refuses, with the status and code of the refusal. */
let refusals = new Map<string, [number, string]>();
let calls: string[] = [];

function route({ url }: RecordedCall): Response {
  const path = new URL(url, "http://test").searchParams.get("path");
  calls.push(path ?? "(roots)");
  const refusal = refusals.get(path ?? "(roots)");
  if (refusal !== undefined) {
    const [status, code] = refusal;
    return jsonResponse(status, {
      error: { code, message: `server says ${code}` },
    });
  }
  if (path === null) return jsonResponse(200, ROOTS);
  const listing = LISTINGS.get(path);
  return listing === undefined
    ? jsonResponse(404, { error: { code: "not_found", message: "no folder" } })
    : jsonResponse(200, listing);
}

afterEach(() => {
  vi.restoreAllMocks();
  refusals = new Map();
  calls = [];
});

function Form({
  initial = "",
  canBrowse = true,
  withMemory = true,
}: {
  initial?: string;
  canBrowse?: boolean;
  withMemory?: boolean;
}) {
  const [path, setPath] = useState(initial);
  const [memory, setMemory] = useState("256");
  return (
    <form>
      <Field
        label="Source folder"
        hint={canBrowse ? BROWSE_PATH_HINT : TYPE_PATH_HINT}
      >
        {(control) => (
          <SourceFolderInput
            control={control}
            value={path}
            onValueChange={setPath}
            canBrowse={canBrowse}
            onUseMemory={
              withMemory
                ? (memoryMb) => {
                    setMemory(String(memoryMb));
                  }
                : undefined
            }
          />
        )}
      </Field>
      <label htmlFor="memory">Memory</label>
      <input
        id="memory"
        value={memory}
        onChange={(event) => {
          setMemory(event.target.value);
        }}
      />
    </form>
  );
}

function setup(props: Parameters<typeof Form>[0] = {}) {
  stubFetch(route);
  const user = userEvent.setup();
  render(<Form {...props} />);
  return { user };
}

function field(): HTMLElement {
  return screen.getByRole("textbox", { name: "Source folder" });
}

function browser(): HTMLElement {
  return screen.getByRole("dialog", { name: "Browse the workspace" });
}

async function openBrowser(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Browse" }));
  return browser();
}

function options(): string[] {
  return within(within(browser()).getByRole("listbox"))
    .queryAllByRole("option")
    .map((option) => option.textContent);
}

describe("SourceFolderInput", () => {
  it("starts at the roots, opens a folder, and fills the field with Use this folder", async () => {
    const { user } = setup();
    const panel = await openBrowser(user);

    await within(panel).findByRole("option", { name: /My folder/ });
    expect(options()).toEqual([
      `My folder${MY_FOLDER}`,
      "Shared/Workspace/Shared",
    ]);
    expect(
      within(panel).getByRole("button", { name: "Use this folder" }),
    ).toBeDisabled();
    expect(within(panel).getByText("Open a folder first.")).toBeInTheDocument();

    await user.click(within(panel).getByRole("option", { name: /My folder/ }));
    await within(panel).findByRole("option", { name: "archive" });
    // Folders first, by name; the file is listed, dimmed and not selectable.
    expect(options()).toEqual(["archive", "sales-board", "notes.txt(a file)"]);
    expect(
      within(panel).getByRole("option", { name: /notes\.txt/ }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(
      within(
        within(panel).getByRole("navigation", { name: "Folder path" }),
      ).getByText("My folder"),
    ).toHaveAttribute("aria-current", "location");

    await user.click(
      within(panel).getByRole("button", { name: "Use this folder" }),
    );
    expect(field()).toHaveValue(MY_FOLDER);
    expect(
      screen.queryByRole("dialog", { name: "Browse the workspace" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      `Source folder set to ${MY_FOLDER}`,
    );
  });

  it("opens at the folder the field names, and goes up with the button and the crumbs", async () => {
    const { user } = setup({ initial: `${MY_FOLDER}/archive/` });
    const panel = await openBrowser(user);
    await within(panel).findByText(
      "This folder is empty. Use it as it is, or go up and pick another.",
    );
    expect(calls).toContain(`${MY_FOLDER}/archive`);

    await user.click(
      within(panel).getByRole("button", { name: "Up one folder" }),
    );
    await within(panel).findByRole("option", { name: "sales-board" });

    await user.click(within(panel).getByRole("button", { name: "Workspace" }));
    await within(panel).findByRole("option", { name: /Shared/ });
  });

  it("moves with the arrows, opens with Enter, goes up with Backspace and closes with Escape", async () => {
    const { user } = setup({ initial: MY_FOLDER });
    const panel = await openBrowser(user);
    await within(panel).findByRole("option", { name: "archive" });
    expect(within(panel).getByRole("listbox")).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    expect(
      within(panel).getByRole("option", { name: "sales-board" }),
    ).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");
    await within(panel).findByRole("option", { name: "server" });

    await user.keyboard("{Backspace}");
    await within(panel).findByRole("option", { name: "archive" });

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "Browse the workspace" }),
    ).not.toBeInTheDocument();
  });

  it("marks a folder that looks like a Node.js app, names its files, and fills the memory with its figure", async () => {
    const { user } = setup({ initial: `${MY_FOLDER}/sales-board` });
    const panel = await openBrowser(user);
    expect(
      await within(panel).findByText(/This folder looks like a Node\.js app/),
    ).toHaveTextContent(/about 768 MB/);
    const markers = within(panel).getByRole("list", {
      name: "Files that mark it",
    });
    expect(
      within(markers)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["app.yaml", "package.json"]);

    await user.click(within(panel).getByRole("button", { name: "Use 768 MB" }));
    expect(screen.getByRole("textbox", { name: "Memory" })).toHaveValue("768");
  });

  it("offers no memory button on a form without a memory field", async () => {
    const { user } = setup({
      initial: `${MY_FOLDER}/sales-board`,
      withMemory: false,
    });
    const panel = await openBrowser(user);
    await within(panel).findByText(/This folder looks like a Node\.js app/);
    expect(
      within(panel).queryByRole("button", { name: "Use 768 MB" }),
    ).not.toBeInTheDocument();
  });

  it("says when a folder holds more entries than listed", async () => {
    LISTINGS.set(
      "/Workspace/Shared/big",
      folder("/Workspace/Shared/big", {
        truncated: true,
        entries: [
          { name: "a", path: "/Workspace/Shared/big/a", kind: "folder" },
        ],
      }),
    );
    const { user } = setup({ initial: "/Workspace/Shared/big" });
    const panel = await openBrowser(user);
    expect(
      await within(panel).findByText(/only the first 1 are listed/),
    ).toBeInTheDocument();
  });

  it.each([
    [403, "session_expired", "Reload", /sign in again/],
    [403, "user_token_missing", "Reload", /Reload the page/],
    [403, "no_access", "Up", /ask the folder's owner/],
    [404, "not_found", "Start", /Go back to the start/],
    [503, "workspace_unavailable", "Retry", /Try again in a moment/],
  ])(
    "shows a %i %s refusal in the server's words, with what to do",
    async (status, code, action, hint) => {
      refusals.set(`${MY_FOLDER}/archive`, [status, code]);
      const { user } = setup({ initial: `${MY_FOLDER}/archive` });
      const panel = await openBrowser(user);
      const alert = await within(panel).findByRole("alert");
      expect(alert).toHaveTextContent(`The server said: server says ${code}`);
      expect(alert).toHaveTextContent(hint);
      expect(within(alert).getByRole("button", { name: action })).toBeEnabled();
      expect(
        within(panel).getByRole("button", { name: "Use this folder" }),
      ).toBeDisabled();
    },
  );

  it("follows a refusal's way out: Up opens the folder above, Retry asks again", async () => {
    refusals.set(`${MY_FOLDER}/archive`, [403, "no_access"]);
    const { user } = setup({ initial: `${MY_FOLDER}/archive` });
    const panel = await openBrowser(user);
    const alert = await within(panel).findByRole("alert");
    await user.click(within(alert).getByRole("button", { name: "Up" }));
    await within(panel).findByRole("option", { name: "archive" });

    refusals.set(MY_FOLDER, [503, "workspace_unavailable"]);
    await user.click(within(panel).getByRole("button", { name: "Workspace" }));
    await user.click(
      await within(panel).findByRole("option", { name: /My folder/ }),
    );
    const unavailable = await within(panel).findByRole("alert");
    refusals.delete(MY_FOLDER);
    await user.click(
      within(unavailable).getByRole("button", { name: "Retry" }),
    );
    await within(panel).findByRole("option", { name: "archive" });
  });

  it("keeps the field free to type in, before and after a refusal", async () => {
    refusals.set("(roots)", [403, "user_token_missing"]);
    const { user } = setup();
    await user.type(field(), "/Workspace/Shared/x");
    expect(field()).toHaveValue("/Workspace/Shared/x");

    refusals.set("/Workspace/Shared/x", [404, "not_found"]);
    const panel = await openBrowser(user);
    await within(panel).findByRole("alert");
    await user.keyboard("{Escape}");
    await user.type(field(), "y");
    expect(field()).toHaveValue("/Workspace/Shared/xy");
  });

  it("offers no Browse to a person who may not browse, and says to type the path", async () => {
    setup({ canBrowse: false });
    expect(
      screen.queryByRole("button", { name: "Browse" }),
    ).not.toBeInTheDocument();
    expect(field()).toHaveAccessibleDescription(TYPE_PATH_HINT);
    await waitFor(() => {
      expect(calls).toEqual([]);
    });
  });
});
