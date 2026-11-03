import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { SettingsGroups } from "@/components/platform/settings-section";
import { ApiError, NETWORK_ERROR, putSetting } from "@/lib/api";
import type { PlatformSetting } from "@/lib/api";
import { platformView, renderInPlatform } from "./shell-fixtures";
import { chooseOption } from "../../support/select";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  putSetting: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
});

const LINK_HREF = "/_appmux/platform/apps/";

/**
 * The settings with a link beside them, like the tabs of the platform area.
 * `followed` is called when a click reaches the link (the page did not hold it
 * back); it cancels the click itself because jsdom cannot navigate.
 */
function Page({
  view = platformView(),
  followed,
  navigate,
  href = LINK_HREF,
  target,
}: {
  view?: ReturnType<typeof platformView>;
  followed: () => void;
  navigate?: (url: string) => void;
  href?: string;
  target?: string;
}) {
  return (
    <>
      <a
        href={href}
        target={target}
        onClick={(event) => {
          event.preventDefault();
          followed();
        }}
      >
        Apps tab
      </a>
      <SettingsGroups view={view} navigate={navigate} />
    </>
  );
}

function setup() {
  const user = userEvent.setup();
  const navigate = vi.fn();
  const followed = vi.fn();
  const page = renderInPlatform(
    <Page followed={followed} navigate={navigate} />,
  );
  return {
    user,
    navigate,
    followed,
    rerenderWith: (view: ReturnType<typeof platformView>) => {
      page.rerenderInPlatform(
        <Page view={view} followed={followed} navigate={navigate} />,
      );
    },
    ...page,
  };
}

/** The row of one setting, found by its name. */
function rowOf(key: string) {
  const row = screen.getByText(key, { selector: "label" }).closest("li");
  if (row === null) throw new Error(`The label ${key} is not inside a row`);
  return within(row);
}

function control(key: string) {
  return screen.getByLabelText(key);
}

function saved(key: string, value: PlatformSetting["value"]): PlatformSetting {
  return {
    key,
    value,
    updated_by: "dana@example.com",
    updated_at: "2026-10-31T08:00:00Z",
  };
}

async function typeInto(
  user: ReturnType<typeof userEvent.setup>,
  key: string,
  text: string,
) {
  const input = control(key);
  await user.clear(input);
  if (text !== "") await user.type(input, text);
}

function bar() {
  return screen.queryByRole("region", { name: "Unsaved changes" });
}

describe("SettingsGroups", () => {
  it("lists every setting as a row with its meaning and its current value, and no save bar", () => {
    setup();
    const row = rowOf("request_timeout_s");
    expect(row.getByText(/Per-request timeout/)).toBeInTheDocument();
    expect(row.getByText("120 s")).toBeInTheDocument();
    expect(row.getByText(/reaches all children/)).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(13);
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(bar()).toBeNull();
  });

  it("shows the bar with a count and marks the changed row, until the change is discarded", async () => {
    const { user } = setup();
    await typeInto(user, "request_timeout_s", "200");

    expect(
      within(bar()!).getByText("1 setting changed, not saved yet"),
    ).toBeInTheDocument();
    expect(rowOf("request_timeout_s").getByText("Changed")).toBeInTheDocument();
    expect(rowOf("memory_reserve_mb").queryByText("Changed")).toBeNull();

    await typeInto(user, "build_timeout_s", "900");
    expect(
      within(bar()!).getByText("2 settings changed, not saved yet"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(bar()).toBeNull();
    expect(control("request_timeout_s")).toHaveValue("120");
    expect(control("build_timeout_s")).toHaveValue("600");
    expect(putSetting).not.toHaveBeenCalled();
  });

  it("clears the bar when a change is typed back to the stored value", async () => {
    const { user } = setup();
    await typeInto(user, "request_timeout_s", "200");
    expect(bar()).not.toBeNull();
    await typeInto(user, "request_timeout_s", "120");
    expect(bar()).toBeNull();
  });

  it("saves one setting with one call, announces it and asks the page to refresh", async () => {
    vi.mocked(putSetting).mockResolvedValue(saved("request_timeout_s", 200));
    const { user, announce, refresh } = setup();
    await typeInto(user, "request_timeout_s", "200");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(putSetting).toHaveBeenCalledExactlyOnceWith("request_timeout_s", {
      value: 200,
    });
    expect(await screen.findByText("Saved 1 setting")).toBeInTheDocument();
    expect(bar()).toBeNull();
    expect(rowOf("request_timeout_s").queryByText("Changed")).toBeNull();
    expect(announce).toHaveBeenCalledWith("Saved 1 setting");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("sends one call per changed setting, one after the other, in the order of the page", async () => {
    let inFlight = 0;
    let mostInFlight = 0;
    vi.mocked(putSetting).mockImplementation(async (key) => {
      inFlight += 1;
      mostInFlight = Math.max(mostInFlight, inFlight);
      await Promise.resolve();
      inFlight -= 1;
      return saved(key, 0);
    });
    const { user } = setup();
    await typeInto(user, "memory_reserve_mb", "512");
    await typeInto(user, "request_timeout_s", "200");
    await chooseOption(user, control("child_cpu_pin"), "Off");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Saved 3 settings")).toBeInTheDocument();
    expect(vi.mocked(putSetting).mock.calls).toEqual([
      ["request_timeout_s", { value: 200 }],
      ["memory_reserve_mb", { value: 512 }],
      ["child_cpu_pin", { value: false }],
    ]);
    expect(mostInFlight).toBe(1);
  });

  it("keeps the rows that failed changed, says why on each, and counts what went through", async () => {
    vi.mocked(putSetting).mockImplementation((key) => {
      if (key === "memory_reserve_mb") {
        return Promise.reject(
          new ApiError(
            409,
            "changed_by_others",
            "memory_reserve_mb was changed by someone else",
          ),
        );
      }
      return Promise.resolve(saved(key, 0));
    });
    const { user, refresh } = setup();
    await typeInto(user, "request_timeout_s", "200");
    await typeInto(user, "memory_reserve_mb", "512");
    await typeInto(user, "build_timeout_s", "900");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await within(bar()!).findByText(/2 of 3 saved, 1 failed/),
    ).toBeInTheDocument();
    // Saved rows are clean; the failed one still holds the typed value and the reason.
    expect(rowOf("request_timeout_s").queryByText("Changed")).toBeNull();
    expect(rowOf("build_timeout_s").queryByText("Changed")).toBeNull();
    expect(rowOf("memory_reserve_mb").getByText("Changed")).toBeInTheDocument();
    expect(control("memory_reserve_mb")).toHaveValue("512");
    expect(control("memory_reserve_mb")).toBeInvalid();
    expect(control("memory_reserve_mb")).toHaveAccessibleDescription(
      /Changed by someone else.*was changed by someone else.*Reload to see the current state/,
    );
    // The person is taken to the first thing to fix, and the page fetched the saved values.
    expect(control("memory_reserve_mb")).toHaveFocus();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(
      within(bar()!).getByText("1 setting changed, not saved yet"),
    ).toBeInTheDocument();
  });

  it("keeps everything and says so when the network is down", async () => {
    vi.mocked(putSetting).mockRejectedValue(
      new ApiError(0, NETWORK_ERROR, "network error"),
    );
    const { user, refresh } = setup();
    await typeInto(user, "request_timeout_s", "200");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await within(bar()!).findByText(/0 of 1 saved, 1 failed/),
    ).toBeInTheDocument();
    expect(control("request_timeout_s")).toHaveValue("200");
    expect(control("request_timeout_s")).toHaveAccessibleDescription(
      /Could not reach appmux/,
    );
    expect(refresh).not.toHaveBeenCalled();
  });

  it.each([
    [400, "The change was not accepted"],
    [403, "You do not have permission"],
    [503, "appmux is not available right now"],
  ])(
    "shows a %i from the server on the row and keeps the value",
    async (status, heading) => {
      vi.mocked(putSetting).mockRejectedValue(
        new ApiError(status, "refused", "the server's reason"),
      );
      const { user } = setup();
      await typeInto(user, "max_request_body_mb", "50");
      await user.click(screen.getByRole("button", { name: "Save changes" }));

      expect(await screen.findByText(new RegExp(heading))).toBeInTheDocument();
      expect(control("max_request_body_mb")).toHaveValue("50");
      expect(
        rowOf("max_request_body_mb").getByText("Changed"),
      ).toBeInTheDocument();
      expect(control("max_request_body_mb")).toHaveAccessibleDescription(
        /the server's reason/,
      );
    },
  );

  it("refuses an invalid value on its row and in the bar, sends nothing, and goes to the row", async () => {
    const { user } = setup();
    await typeInto(user, "memory_reserve_mb", "512");
    await typeInto(user, "request_timeout_s", "300");

    expect(control("request_timeout_s")).toBeInvalid();
    expect(control("request_timeout_s")).toHaveAccessibleDescription(
      /must be at most 290/,
    );
    expect(
      within(bar()!).getByText(/1 setting needs fixing before you can save/),
    ).toHaveTextContent("request_timeout_s must be at most 290");

    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(putSetting).not.toHaveBeenCalled();
    expect(control("request_timeout_s")).toHaveFocus();
  });

  it.each([
    ["max_locked_releases", "7", /must be at most 6/],
    ["max_releases_per_app", "6", /must be at least 7/],
    ["memory_reserve_mb", "12x", /must be a whole number/],
    ["build_timeout_s", "", /is required/],
  ])("explains %s = %j on its row", async (key, typed, message) => {
    const { user } = setup();
    await typeInto(user, key, typed);
    expect(control(key)).toBeInvalid();
    expect(control(key)).toHaveAccessibleDescription(message);
  });

  it("checks the two release settings against each other as they are being entered", async () => {
    vi.mocked(putSetting).mockImplementation((key) =>
      Promise.resolve(saved(key, 0)),
    );
    const { user } = setup();
    await typeInto(user, "max_locked_releases", "12");
    expect(control("max_locked_releases")).toBeInvalid();

    await typeInto(user, "max_releases_per_app", "20");
    expect(control("max_locked_releases")).toBeValid();
    expect(control("max_releases_per_app")).toBeValid();

    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved 2 settings");
    expect(vi.mocked(putSetting).mock.calls).toEqual([
      ["max_releases_per_app", { value: 20 }],
      ["max_locked_releases", { value: 12 }],
    ]);
  });

  it("lowers the locks before the cap when both go down", async () => {
    vi.mocked(putSetting).mockImplementation((key) =>
      Promise.resolve(saved(key, 0)),
    );
    const { user } = setup();
    await typeInto(user, "max_releases_per_app", "6");
    await typeInto(user, "max_locked_releases", "2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved 2 settings");
    expect(vi.mocked(putSetting).mock.calls.map(([key]) => key)).toEqual([
      "max_locked_releases",
      "max_releases_per_app",
    ]);
  });

  it("does not send the second release setting when the first was refused", async () => {
    vi.mocked(putSetting).mockRejectedValue(
      new ApiError(400, "invalid_request", "refused"),
    );
    const { user } = setup();
    await typeInto(user, "max_releases_per_app", "20");
    await typeInto(user, "max_locked_releases", "12");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await within(bar()!).findByText(/0 of 2 saved, 1 failed, 1 not sent/),
    ).toBeInTheDocument();
    expect(putSetting).toHaveBeenCalledTimes(1);
    expect(control("max_locked_releases")).toHaveAccessibleDescription(
      /Not sent: max_releases_per_app was not saved/,
    );
  });

  it("sends the chosen option of a choice and of a switch, and a list as one entry per line", async () => {
    vi.mocked(putSetting).mockImplementation((key) =>
      Promise.resolve(saved(key, 0)),
    );
    const { user } = setup();
    await chooseOption(user, control("package_mode"), "index");
    await user.type(
      control("default_wheel_sources"),
      "{Enter}{Enter}  /Workspace/Shared/more  ",
    );
    expect(control("default_wheel_sources")).toHaveAccessibleDescription(
      /One folder per line\./,
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved 2 settings");

    expect(vi.mocked(putSetting).mock.calls).toEqual([
      ["package_mode", { value: "index" }],
      [
        "default_wheel_sources",
        {
          value: [
            "/Workspace/Shared/appmux/wheelhouse",
            "/Workspace/Shared/more",
          ],
        },
      ],
    ]);
  });

  it("asks for a second look before lending the platform's own token, and saves only on yes", async () => {
    vi.mocked(putSetting).mockResolvedValue(
      saved("shared_sp_delegation_allowed", true),
    );
    const { user } = setup();
    await chooseOption(user, control("shared_sp_delegation_allowed"), "On");

    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(
      within(bar()!).getByRole("group", {
        name: /lend the appmux service principal/,
      }),
    ).toBeInTheDocument();
    expect(putSetting).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "Yes, save 1 change" }),
    );
    expect(putSetting).toHaveBeenCalledExactlyOnceWith(
      "shared_sp_delegation_allowed",
      { value: true },
    );
  });

  it("turning a switch off needs no confirmation", async () => {
    vi.mocked(putSetting).mockResolvedValue(saved("child_cpu_pin", false));
    const { user } = setup();
    await chooseOption(user, control("child_cpu_pin"), "Off");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(within(bar() ?? document.body).queryByRole("group")).toBeNull();
    expect(putSetting).toHaveBeenCalledTimes(1);
  });

  it("notes that another admin changed a row being edited, and keeps the typed value", async () => {
    const { user, rerenderWith } = setup();
    await typeInto(user, "request_timeout_s", "200");

    const view = platformView();
    rerenderWith({
      ...view,
      settings: { ...view.settings, request_timeout_s: 150 },
    });

    expect(control("request_timeout_s")).toHaveValue("200");
    expect(
      rowOf("request_timeout_s").getByText(/Another admin changed this to/),
    ).toBeInTheDocument();
    expect(rowOf("request_timeout_s").getByText("150 s")).toBeInTheDocument();
  });
});

describe("leaving with unsaved changes", () => {
  function tryUnload(): boolean {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  }

  it("asks the browser to confirm closing the tab only while something is unsaved", async () => {
    const { user } = setup();
    expect(tryUnload()).toBe(false);
    await typeInto(user, "request_timeout_s", "200");
    expect(tryUnload()).toBe(true);
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(tryUnload()).toBe(false);
  });

  it("lets a link through while nothing is unsaved, holds it back and asks once something is, and Stay keeps the changes", async () => {
    const { user, navigate, followed } = setup();
    await user.click(screen.getByRole("link", { name: "Apps tab" }));
    expect(followed).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Leave this page\?/)).toBeNull();

    await typeInto(user, "request_timeout_s", "200");
    await user.click(screen.getByRole("link", { name: "Apps tab" }));
    expect(followed).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Leave this page\?/)).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Stay on this page" }),
    ).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Stay on this page" }));
    expect(screen.queryByText(/Leave this page\?/)).toBeNull();
    expect(control("request_timeout_s")).toHaveValue("200");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("leaves, and drops the changes, only after Leave and discard", async () => {
    const { user, navigate } = setup();
    await typeInto(user, "request_timeout_s", "200");
    await user.click(screen.getByRole("link", { name: "Apps tab" }));
    await user.click(screen.getByRole("button", { name: "Leave and discard" }));
    expect(navigate).toHaveBeenCalledExactlyOnceWith(
      expect.stringContaining(LINK_HREF),
    );
    expect(bar()).toBeNull();
  });

  it("does not hold back a link that opens in a new tab or stays on this page", async () => {
    const user = userEvent.setup();
    const followed = vi.fn();
    const { rerenderInPlatform } = renderInPlatform(
      <Page followed={followed} target="_blank" />,
    );
    await typeInto(user, "request_timeout_s", "200");
    await user.click(screen.getByRole("link", { name: "Apps tab" }));
    expect(followed).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Leave this page\?/)).toBeNull();

    rerenderInPlatform(<Page followed={followed} href="#traffic" />);
    await user.click(screen.getByRole("link", { name: "Apps tab" }));
    expect(followed).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/Leave this page\?/)).toBeNull();
  });
});
