import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  CLOSED_SOURCE_VIEW,
  MAX_PREVIEW_CHARS,
  isSafeSourcePath,
  previewFile,
  sourceEntries,
  sourceListingSummary,
  sourceViewReducer,
} from "@/lib/app/source";
import type { SourceViewState } from "@/lib/app/source";
import { readExample } from "../../../support/contract-examples";

const LISTING = api.ReleaseSourceListing.parse(
  readExample("ReleaseSourceListing"),
);
const FILE = api.ReleaseSourceFile.parse(readExample("ReleaseSourceFile"));

function file(
  overrides: Partial<api.ReleaseSourceFile>,
): api.ReleaseSourceFile {
  return { ...FILE, ...overrides };
}

describe("isSafeSourcePath", () => {
  it("accepts a relative path of folders and a file", () => {
    for (const path of [
      "app.py",
      "pages/home.py",
      "a/b/c.txt",
      ".env.example",
    ]) {
      assert.equal(isSafeSourcePath(path), true, path);
    }
  });

  it("refuses what could leave the snapshot or is not a path", () => {
    for (const path of [
      "",
      "/etc/passwd",
      "../secret",
      "a/../b",
      "a/./b",
      "a//b",
      "a/",
      "a\\b",
      "a\nb",
      "a\u0000b",
      ".",
      "..",
    ]) {
      assert.equal(isSafeSourcePath(path), false, JSON.stringify(path));
    }
  });
});

describe("sourceEntries", () => {
  it("sorts by path and splits folder from name", () => {
    const entries = sourceEntries({
      release_no: 7,
      items: [
        { path: "pages/b.py", size_bytes: 10, kind: "text" },
        { path: "app.py", size_bytes: 20, kind: "text" },
        { path: "pages/a.py", size_bytes: 30, kind: "text" },
      ],
    });
    assert.deepEqual(
      entries.map((entry) => [entry.path, entry.directory, entry.name]),
      [
        ["app.py", "", "app.py"],
        ["pages/a.py", "pages", "a.py"],
        ["pages/b.py", "pages", "b.py"],
      ],
    );
  });

  it("opens text files, lists binary ones with the reason, and does not open an unsafe path", () => {
    const entries = sourceEntries({
      release_no: 7,
      items: [
        { path: "logo.png", size_bytes: 4096, kind: "binary" },
        { path: "../escape.py", size_bytes: 1, kind: "text" },
        { path: "app.py", size_bytes: 1, kind: "text" },
      ],
    });
    const byPath = Object.fromEntries(
      entries.map((entry) => [entry.path, entry]),
    );
    assert.equal(byPath["app.py"]?.canOpen, true);
    assert.equal(byPath["logo.png"]?.canOpen, false);
    assert.match(byPath["logo.png"]?.reason ?? "", /binary/i);
    assert.equal(byPath["../escape.py"]?.canOpen, false);
    assert.ok(byPath["../escape.py"]?.reason);
  });

  it("handles the contract's example listing", () => {
    const entries = sourceEntries(LISTING);
    assert.equal(entries.length, LISTING.items.length);
  });
});

describe("sourceListingSummary", () => {
  it("counts files, binary files and the total size", () => {
    const text = sourceListingSummary({
      release_no: 7,
      items: [
        { path: "a.py", size_bytes: 1024, kind: "text" },
        { path: "b.png", size_bytes: 1024, kind: "binary" },
        { path: "c.py", size_bytes: 2048, kind: "text" },
      ],
    });
    assert.equal(text, "3 files (1 binary), 4 KB");
  });

  it("says so for an empty snapshot and for one file", () => {
    assert.equal(
      sourceListingSummary({ release_no: 7, items: [] }),
      "No files",
    );
    assert.equal(
      sourceListingSummary({
        release_no: 7,
        items: [{ path: "a.py", size_bytes: 10, kind: "text" }],
      }),
      "1 file, 10 B",
    );
  });
});

describe("previewFile", () => {
  it("shows a short file whole", () => {
    const preview = previewFile(
      file({ content: "print('hi')\n", size_bytes: 12 }),
    );
    assert.equal(preview.text, "print('hi')\n");
    assert.equal(preview.isCut, false);
    assert.equal(preview.isBinaryLooking, false);
    assert.equal(preview.note, null);
  });

  it("cuts a long file at the cap and says how much is shown", () => {
    const content = "x".repeat(MAX_PREVIEW_CHARS + 500);
    const preview = previewFile(file({ content, size_bytes: content.length }));
    assert.equal(preview.text.length, MAX_PREVIEW_CHARS);
    assert.equal(preview.isCut, true);
    assert.match(preview.note ?? "", /first/i);
  });

  it("does not show content that holds a NUL byte, though the server called it text", () => {
    const preview = previewFile(file({ content: "ab\u0000cd", size_bytes: 5 }));
    assert.equal(preview.isBinaryLooking, true);
    assert.equal(preview.text, "");
    assert.match(preview.note ?? "", /binary/i);
  });

  it("does not cut in the middle of a surrogate pair", () => {
    // The leading "a" puts the cap in the middle of a pair.
    const content = "a" + "😀".repeat(MAX_PREVIEW_CHARS);
    const preview = previewFile(
      file({ content, size_bytes: content.length * 4 }),
    );
    const last = preview.text.charCodeAt(preview.text.length - 1);
    assert.ok(
      !(last >= 0xd800 && last <= 0xdbff),
      "ends on a lone high surrogate",
    );
    assert.equal(preview.isCut, true);
  });
});

describe("sourceViewReducer", () => {
  function opened(releaseNo = 7): SourceViewState {
    return sourceViewReducer(CLOSED_SOURCE_VIEW, { type: "open", releaseNo });
  }
  function listed(releaseNo = 7): SourceViewState {
    return sourceViewReducer(opened(releaseNo), {
      type: "listing-loaded",
      releaseNo,
      listing: { ...LISTING, release_no: releaseNo },
    });
  }

  it("is closed at first", () => {
    assert.equal(CLOSED_SOURCE_VIEW.releaseNo, null);
    assert.equal(CLOSED_SOURCE_VIEW.listing.status, "idle");
  });

  it("opens a release into a loading listing", () => {
    const state = opened();
    assert.equal(state.releaseNo, 7);
    assert.equal(state.listing.status, "loading");
    assert.equal(state.filePath, null);
  });

  it("takes the listing it asked for", () => {
    assert.equal(listed().listing.status, "ready");
  });

  it("drops a listing that arrives for another release", () => {
    const state = sourceViewReducer(opened(7), {
      type: "listing-loaded",
      releaseNo: 6,
      listing: { ...LISTING, release_no: 6 },
    });
    assert.equal(state.listing.status, "loading");
  });

  it("keeps a failed listing with its error", () => {
    const error = new Error("boom");
    const state = sourceViewReducer(opened(), {
      type: "listing-failed",
      releaseNo: 7,
      error,
    });
    assert.deepEqual(state.listing, { status: "failed", error });
  });

  it("opens a file and takes it when it arrives", () => {
    const requested = sourceViewReducer(listed(), {
      type: "file-requested",
      releaseNo: 7,
      path: "app.py",
    });
    assert.equal(requested.filePath, "app.py");
    assert.equal(requested.file.status, "loading");
    const loaded = sourceViewReducer(requested, {
      type: "file-loaded",
      releaseNo: 7,
      path: "app.py",
      file: file({ release_no: 7, path: "app.py" }),
    });
    assert.equal(loaded.file.status, "ready");
  });

  it("drops a file that arrives after the person chose another one", () => {
    const first = sourceViewReducer(listed(), {
      type: "file-requested",
      releaseNo: 7,
      path: "a.py",
    });
    const second = sourceViewReducer(first, {
      type: "file-requested",
      releaseNo: 7,
      path: "b.py",
    });
    const late = sourceViewReducer(second, {
      type: "file-loaded",
      releaseNo: 7,
      path: "a.py",
      file: file({ path: "a.py" }),
    });
    assert.equal(late.filePath, "b.py");
    assert.equal(late.file.status, "loading");
    const lateFailure = sourceViewReducer(second, {
      type: "file-failed",
      releaseNo: 7,
      path: "a.py",
      error: new Error("late"),
    });
    assert.equal(lateFailure.file.status, "loading");
  });

  it("opening another release starts again", () => {
    const state = sourceViewReducer(
      sourceViewReducer(listed(), {
        type: "file-requested",
        releaseNo: 7,
        path: "app.py",
      }),
      { type: "open", releaseNo: 6 },
    );
    assert.equal(state.releaseNo, 6);
    assert.equal(state.filePath, null);
    assert.equal(state.file.status, "idle");
    assert.equal(state.listing.status, "loading");
  });

  it("closes, and closes by itself when the release is gone", () => {
    assert.deepEqual(
      sourceViewReducer(listed(), { type: "close" }),
      CLOSED_SOURCE_VIEW,
    );
    assert.deepEqual(
      sourceViewReducer(listed(), {
        type: "releases-changed",
        releaseNos: [9, 8],
      }),
      CLOSED_SOURCE_VIEW,
    );
    const stays = listed();
    assert.equal(
      sourceViewReducer(stays, {
        type: "releases-changed",
        releaseNos: [9, 7],
      }),
      stays,
    );
  });

  it("goes back from a file to the listing", () => {
    const withFile = sourceViewReducer(listed(), {
      type: "file-requested",
      releaseNo: 7,
      path: "app.py",
    });
    const back = sourceViewReducer(withFile, { type: "file-closed" });
    assert.equal(back.filePath, null);
    assert.equal(back.file.status, "idle");
    assert.equal(back.listing.status, "ready");
  });
});
