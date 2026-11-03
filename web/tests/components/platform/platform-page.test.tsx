import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { PlatformPage } from "@/components/platform/platform-page";
import { ApiError, getMe, getPlatform, listRequests } from "@/lib/api";
import { RequestList } from "@/lib/api";
import type { MeResponse } from "@/lib/api";
import { readExample } from "../../support/contract-examples";
import { platformView } from "./shell-fixtures";

const location = vi.hoisted(() => ({ pathname: "/platform/" }));
vi.mock("next/navigation", () => ({
  usePathname: () => location.pathname,
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  getMe: vi.fn(),
  getPlatform: vi.fn(),
  listRequests: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
  location.pathname = "/platform/";
  // A poll that never answers: the page stays in its loading state.
  vi.mocked(getPlatform).mockReturnValue(new Promise(() => undefined));
  vi.mocked(listRequests).mockReturnValue(new Promise(() => undefined));
});

function me(isPlatformAdmin: boolean): MeResponse {
  return {
    email: "eve@example.com",
    groups: ["data-platform"],
    is_platform_admin: isPlatformAdmin,
  };
}

describe("PlatformPage access", () => {
  it("asks for nothing about the platform while it checks who is looking", () => {
    vi.mocked(getMe).mockReturnValue(new Promise(() => undefined));
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );
    expect(
      screen.getByRole("heading", { name: "Platform", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("Checking your access…")).toBeInTheDocument();
    expect(getPlatform).not.toHaveBeenCalled();
    expect(listRequests).not.toHaveBeenCalled();
  });

  it("explains to a visitor who is not a platform admin and never loads the platform", async () => {
    vi.mocked(getMe).mockResolvedValue(me(false));
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );
    expect(
      await screen.findByText("This page is for platform admins"),
    ).toBeInTheDocument();
    expect(screen.getByText("eve@example.com")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Back to the launcher" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh now" })).toBeNull();
    expect(getPlatform).not.toHaveBeenCalled();
    expect(listRequests).not.toHaveBeenCalled();
  });

  it("loads the platform, with the abort signal, once GET /me says admin", async () => {
    vi.mocked(getMe).mockResolvedValue(me(true));
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );
    expect(await screen.findByText("Loading the platform")).toHaveAttribute(
      "role",
      "status",
    );
    expect(
      screen.getByRole("button", { name: "Refresh now" }),
    ).toBeInTheDocument();
    // Each poll hands its call the abort signal, so unmounting cancels the request.
    for (const load of [getPlatform, listRequests]) {
      const [options] = vi.mocked(load).mock.calls[0] ?? [];
      expect(options?.signal).toBeInstanceOf(AbortSignal);
    }
  });

  it("shows the page in a panel beside the sidebar of the five areas, with a link back to all apps", async () => {
    vi.mocked(getMe).mockResolvedValue(me(true));
    vi.mocked(getPlatform).mockResolvedValue(platformView());
    vi.mocked(listRequests).mockResolvedValue({ items: [] });
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );
    expect(
      await screen.findByText("A page of the platform"),
    ).toBeInTheDocument();
    const sidebar = screen.getByRole("navigation", {
      name: "Platform sections",
    });
    expect(
      within(sidebar)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["All apps", "Overview", "Apps", "Access", "Egress", "Settings"]);
    expect(
      within(sidebar).getByRole("link", { name: "All apps" }),
    ).toHaveAttribute("href", "/");
  });

  it("marks the area on screen as the current page, with or without the trailing slash", async () => {
    location.pathname = "/platform/egress";
    vi.mocked(getMe).mockResolvedValue(me(true));
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );
    const sidebar = await screen.findByRole("navigation", {
      name: "Platform sections",
    });
    expect(
      within(sidebar)
        .getAllByRole("link")
        .filter((link) => link.getAttribute("aria-current") === "page")
        .map((link) => link.textContent),
    ).toEqual(["Egress"]);
  });

  it("counts the requests waiting on Overview, and on no other area", async () => {
    vi.mocked(getMe).mockResolvedValue(me(true));
    vi.mocked(getPlatform).mockResolvedValue(platformView());
    // Three of the example's four requests are pending.
    vi.mocked(listRequests).mockResolvedValue(
      RequestList.parse(readExample("RequestList")),
    );
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );
    const sidebar = await screen.findByRole("navigation", {
      name: "Platform sections",
    });
    // The count is read as words, not as "Overview3".
    expect(
      await within(sidebar).findByRole("link", {
        name: "Overview, 3 requests waiting",
      }),
    ).toHaveTextContent(/^Overview3/);
    expect(
      within(sidebar).getByRole("link", { name: "Apps" }),
    ).toHaveTextContent(/^Apps$/);
  });

  it("keeps the page back from a visitor who is not an admin", async () => {
    vi.mocked(getMe).mockResolvedValue(me(false));
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );
    await screen.findByText("This page is for platform admins");
    expect(screen.queryByText("A page of the platform")).toBeNull();
    expect(
      screen.queryByRole("navigation", { name: "Platform sections" }),
    ).toBeNull();
  });

  it("fails closed when GET /me fails, and Try again asks it again", async () => {
    vi.mocked(getMe)
      .mockRejectedValueOnce(new ApiError(503, "unavailable", "starting up"))
      .mockResolvedValueOnce(me(false));
    const user = userEvent.setup();
    render(
      <PlatformPage>
        <p>A page of the platform</p>
      </PlatformPage>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not check your access");
    expect(getPlatform).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => {
      expect(
        screen.getByText("This page is for platform admins"),
      ).toBeInTheDocument();
    });
    expect(getMe).toHaveBeenCalledTimes(2);
  });
});
