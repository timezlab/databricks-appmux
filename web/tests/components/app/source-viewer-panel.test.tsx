import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as Api from "@/lib/api";
import { ApiError, ReleaseSourceListing } from "@/lib/api";
import type { ReleaseSourceFile } from "@/lib/api";
import { SourceViewerPanel } from "@/components/app/source-viewer-panel";
import { useSourceViewer } from "@/components/app/use-source-viewer";
import { readExample } from "../../support/contract-examples";

const { listReleaseSource, getReleaseSourceFile } = vi.hoisted(() => ({
  listReleaseSource: vi.fn(),
  getReleaseSourceFile: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof Api>()),
  listReleaseSource,
  getReleaseSourceFile,
}));

const listing = ReleaseSourceListing.parse(readExample("ReleaseSourceListing"));

function fileOf(path: string, content: string): ReleaseSourceFile {
  return {
    release_no: 7,
    path,
    size_bytes: content.length,
    content,
  };
}

/** The viewer as the release section wires it: a button opens it, the panel shows it. */
function Harness({ onReloadApp = vi.fn() }: { onReloadApp?: () => void }) {
  const viewer = useSourceViewer("sales", [7]);
  return (
    <>
      <button
        type="button"
        onClick={() => {
          viewer.open(7);
        }}
      >
        Show source
      </button>
      <SourceViewerPanel
        slug="sales"
        viewer={viewer}
        onReloadApp={onReloadApp}
      />
    </>
  );
}

beforeEach(() => {
  listReleaseSource.mockReset();
  getReleaseSourceFile.mockReset();
});

describe("SourceViewerPanel", () => {
  it("fetches nothing until a person asks: every list and file read is audited", async () => {
    listReleaseSource.mockResolvedValue(listing);
    const user = userEvent.setup();
    render(<Harness />);
    expect(listReleaseSource).not.toHaveBeenCalled();
    expect(screen.queryByRole("region")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Show source" }));
    expect(await screen.findByText(/5 files \(1 binary\)/)).toBeInTheDocument();
    expect(listReleaseSource).toHaveBeenCalledExactlyOnceWith("sales", 7);
    expect(getReleaseSourceFile).not.toHaveBeenCalled();
  });

  it("shows the chosen file's text, and the file list again after Back", async () => {
    listReleaseSource.mockResolvedValue(listing);
    getReleaseSourceFile.mockResolvedValue(
      fileOf("app.yaml", "command: [uvicorn]\nenv:\n  - name: LOG_LEVEL\n"),
    );
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Show source" }));
    await user.click(
      await screen.findByRole("button", { name: "Open app.yaml" }),
    );

    const text = await screen.findByRole("region", {
      name: "Contents of app.yaml",
    });
    expect(text).toHaveTextContent("command: [uvicorn]");
    expect(text).toHaveTextContent("name: LOG_LEVEL");
    expect(getReleaseSourceFile).toHaveBeenCalledExactlyOnceWith(
      "sales",
      7,
      "app.yaml",
    );

    // The scrolling block is a tab stop, so a keyboard can scroll long lines.
    for (
      let presses = 0;
      presses < 5 && text !== document.activeElement;
      presses++
    ) {
      await user.tab();
    }
    expect(text).toHaveFocus();

    await user.click(
      screen.getByRole("button", { name: "Back to the file list" }),
    );
    expect(
      screen.getByRole("button", { name: "Open app.yaml" }),
    ).toBeInTheDocument();
  });

  it("draws file content as text, never as markup", async () => {
    const hostile = '<script>alert("x")</script><img src=x onerror=alert(1)>';
    listReleaseSource.mockResolvedValue(listing);
    getReleaseSourceFile.mockResolvedValue(fileOf("app.py", hostile));
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Show source" }));
    await user.click(
      await screen.findByRole("button", { name: "Open app.py" }),
    );

    const text = await screen.findByRole("region", {
      name: "Contents of app.py",
    });
    expect(text.textContent).toBe(hostile);
    expect(text.querySelector("script, img")).toBeNull();
  });

  it("lists binary files and unsafe paths without a way to open them", async () => {
    const hostile = {
      release_no: 7,
      items: [
        { path: "static/logo.png", size_bytes: 20480, kind: "binary" },
        { path: "../outside.txt", size_bytes: 10, kind: "text" },
        { path: "app.py", size_bytes: 1840, kind: "text" },
      ],
    };
    listReleaseSource.mockResolvedValue(ReleaseSourceListing.parse(hostile));
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Show source" }));

    const files = await screen.findByRole("list");
    expect(within(files).getAllByRole("button")).toHaveLength(1);
    expect(
      within(files).getByRole("button", { name: "Open app.py" }),
    ).toBeInTheDocument();
    expect(
      within(files).getByText("Binary files are listed, not shown."),
    ).toBeInTheDocument();
    expect(
      within(files).getByText("This path cannot be opened from here."),
    ).toBeInTheDocument();
  });

  it("says a file that looks binary is not shown, instead of drawing it", async () => {
    listReleaseSource.mockResolvedValue(listing);
    getReleaseSourceFile.mockResolvedValue(fileOf("app.py", "MZ\u0000\u0001"));
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Show source" }));
    await user.click(
      await screen.findByRole("button", { name: "Open app.py" }),
    );

    expect(
      await screen.findByText("This file looks binary, so it is not shown."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Contents of app.py" }),
    ).not.toBeInTheDocument();
  });

  it("announces a failed listing and reads it again on Try again", async () => {
    listReleaseSource
      .mockRejectedValueOnce(
        new ApiError(503, "unavailable", "the snapshot store is down"),
      )
      .mockResolvedValueOnce(listing);
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Show source" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not list the files",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/5 files/)).toBeInTheDocument();
    expect(listReleaseSource).toHaveBeenCalledTimes(2);
  });
});
