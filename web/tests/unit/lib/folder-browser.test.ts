import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { ApiError, NETWORK_ERROR } from "@/lib/api";
import type { AppSummary, MeResponse, WorkspaceListing } from "@/lib/api";
import {
  breadcrumb,
  browseRefusal,
  folderHint,
  mayBrowseWorkspace,
  orderEntries,
  startPath,
  upTarget,
} from "@/lib/folder-browser";

const ROOTS = [
  { label: "My folder", path: "/Workspace/Users/alice@example.com" },
  { label: "Shared", path: "/Workspace/Shared" },
];

function listing(overrides: Partial<WorkspaceListing> = {}): WorkspaceListing {
  return {
    path: "/Workspace/Shared/apps",
    parent: "/Workspace/Shared",
    roots: [],
    entries: [],
    truncated: false,
    markers: [],
    looks_like_app: false,
    runtime_hint: null,
    suggested_memory_mb: null,
    ...overrides,
  };
}

describe("startPath", () => {
  it("opens at the folder the field names, without a trailing slash", () => {
    assert.equal(
      startPath("  /Workspace/Shared/apps/wiki/ "),
      "/Workspace/Shared/apps/wiki",
    );
  });

  it("opens at the roots for an empty field, a path outside /Workspace/ or /Workspace/ itself", () => {
    assert.equal(startPath(""), null);
    assert.equal(startPath("Shared/apps"), null);
    assert.equal(startPath("/Workspace/"), null);
    assert.equal(startPath("/Workspace"), null);
  });
});

describe("breadcrumb", () => {
  it("is only the start at the roots", () => {
    assert.deepEqual(breadcrumb(null, ROOTS), [
      { label: "Workspace", path: null },
    ]);
  });

  it("names a known root by its label, then one crumb per folder below it", () => {
    assert.deepEqual(
      breadcrumb(
        "/Workspace/Users/alice@example.com/sales-board/server",
        ROOTS,
      ),
      [
        { label: "Workspace", path: null },
        { label: "My folder", path: "/Workspace/Users/alice@example.com" },
        {
          label: "sales-board",
          path: "/Workspace/Users/alice@example.com/sales-board",
        },
        {
          label: "server",
          path: "/Workspace/Users/alice@example.com/sales-board/server",
        },
      ],
    );
  });

  it("does not take a sibling whose name starts like a root for that root", () => {
    assert.deepEqual(
      breadcrumb("/Workspace/Shared-old/x", ROOTS).map((crumb) => crumb.label),
      ["Workspace", "Shared-old", "x"],
    );
  });

  it("splits a path under no known root into its folders below /Workspace", () => {
    assert.deepEqual(breadcrumb("/Workspace/Repos/team/app", []), [
      { label: "Workspace", path: null },
      { label: "Repos", path: "/Workspace/Repos" },
      { label: "team", path: "/Workspace/Repos/team" },
      { label: "app", path: "/Workspace/Repos/team/app" },
    ]);
  });
});

describe("upTarget", () => {
  it("has nowhere to go from the roots", () => {
    assert.equal(upTarget(listing({ path: null, parent: null })), null);
  });

  it("goes to the parent, or back to the roots when the server gives none", () => {
    assert.deepEqual(upTarget(listing()), { path: "/Workspace/Shared" });
    assert.deepEqual(
      upTarget(listing({ path: "/Workspace/Shared", parent: null })),
      { path: null },
    );
  });
});

describe("orderEntries", () => {
  it("puts folders first, then files, each by name without regard to case", () => {
    const ordered = orderEntries([
      { name: "b.txt", path: "/Workspace/x/b.txt", kind: "file" },
      { name: "zeta", path: "/Workspace/x/zeta", kind: "folder" },
      { name: "App.yaml", path: "/Workspace/x/App.yaml", kind: "file" },
      { name: "alpha", path: "/Workspace/x/alpha", kind: "folder" },
      { name: "Beta", path: "/Workspace/x/Beta", kind: "folder" },
    ]);
    assert.deepEqual(
      ordered.map((entry) => entry.name),
      ["alpha", "Beta", "zeta", "App.yaml", "b.txt"],
    );
  });
});

describe("folderHint", () => {
  it("says nothing for the roots or a folder that does not look like an app", () => {
    assert.equal(folderHint(listing({ path: null })), null);
    assert.equal(folderHint(listing()), null);
  });

  it("says a Node.js app needs about the suggested memory, and offers it", () => {
    const hint = folderHint(
      listing({
        looks_like_app: true,
        markers: ["app.yaml", "package.json"],
        runtime_hint: "node",
        suggested_memory_mb: 768,
      }),
    );
    assert.equal(hint?.suggestedMemoryMb, 768);
    assert.match(hint?.text ?? "", /Node\.js app/);
    assert.match(hint?.text ?? "", /about 768 MB/);
  });

  it("names a Python app or an app of unknown runtime without a memory figure", () => {
    const python = folderHint(
      listing({ looks_like_app: true, runtime_hint: "python" }),
    );
    assert.equal(python?.text, "This folder looks like a Python app.");
    assert.equal(python?.suggestedMemoryMb, null);
    assert.equal(
      folderHint(listing({ looks_like_app: true, markers: ["app.yaml"] }))
        ?.text,
      "This folder looks like an app.",
    );
  });
});

describe("browseRefusal", () => {
  function refused(status: number, code: string) {
    return browseRefusal(new ApiError(status, code, `server says ${code}`));
  }

  it("keeps the server's words and says what to do for each refusal of the route", () => {
    const cases: [number, string, string][] = [
      [403, "session_expired", "reload-page"],
      [403, "user_token_missing", "reload-page"],
      [403, "no_access", "up"],
      [404, "not_found", "start"],
      [503, "workspace_unavailable", "retry"],
      [400, "invalid_path", "start"],
      [403, "forbidden", "type"],
    ];
    for (const [status, code, recovery] of cases) {
      const refusal = refused(status, code);
      assert.equal(refusal.detail, `server says ${code}`, code);
      assert.equal(refusal.recovery, recovery, code);
      assert.ok(refusal.hint.length > 0, code);
    }
    assert.match(refused(403, "session_expired").hint, /sign in again/);
  });

  it("offers a retry when appmux cannot be reached or the code is unknown", () => {
    const offline = browseRefusal(new ApiError(0, NETWORK_ERROR, "offline"));
    assert.equal(offline.recovery, "retry");
    assert.equal(offline.detail, null);
    assert.equal(refused(500, "internal").recovery, "retry");
    assert.equal(browseRefusal(new Error("bug")).recovery, "retry");
  });
});

describe("mayBrowseWorkspace", () => {
  const me = (isPlatformAdmin: boolean): MeResponse => ({
    email: "alice@example.com",
    groups: [],
    is_platform_admin: isPlatformAdmin,
  });
  const summary = (permission: AppSummary["permission"]): AppSummary => ({
    slug: "sales",
    display_name: "Sales",
    state: "running",
    permission,
    open_modes: [],
  });

  it("lets a platform admin and a person who manages an app browse", () => {
    assert.equal(mayBrowseWorkspace(me(true), []), true);
    assert.equal(
      mayBrowseWorkspace(me(false), [
        summary("CAN_USE"),
        summary("CAN_MANAGE"),
      ]),
      true,
    );
  });

  it("keeps a plain visitor, and anyone while the answers are missing, to typing", () => {
    assert.equal(mayBrowseWorkspace(me(false), [summary("CAN_USE")]), false);
    assert.equal(mayBrowseWorkspace(null, []), false);
    assert.equal(mayBrowseWorkspace(me(false), null), false);
  });
});
