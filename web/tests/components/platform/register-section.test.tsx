import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { RegisterSection } from "@/components/platform/register-section";
import { ApiError, registerApp } from "@/lib/api";
import { appDetail, platformView, renderInPlatform } from "./shell-fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  registerApp: vi.fn(),
}));

const SLUG_RULE = "lower-case letters, digits and -, at most 31 characters";
const EMAIL_RULE = "must be an email address (name@domain)";

beforeEach(() => {
  vi.resetAllMocks();
});

function setup() {
  const user = userEvent.setup();
  const page = renderInPlatform(<RegisterSection view={platformView()} />);
  return { user, ...page };
}

type User = ReturnType<typeof userEvent.setup>;

async function fill(
  user: User,
  values: {
    slug: string;
    name: string;
    source: string;
    memory: string;
    owner: string;
  },
): Promise<void> {
  await user.type(screen.getByRole("textbox", { name: /^Slug/ }), values.slug);
  await user.type(
    screen.getByRole("textbox", { name: /^Display name/ }),
    values.name,
  );
  await user.type(
    screen.getByRole("textbox", { name: /^Source folder/ }),
    values.source,
  );
  await user.type(
    screen.getByRole("textbox", { name: /^Memory budget/ }),
    values.memory,
  );
  await user.type(
    screen.getByRole("textbox", { name: /^Owner/ }),
    values.owner,
  );
}

const VALID = {
  slug: "inventory",
  name: "Inventory",
  source: "/Workspace/Shared/apps/inventory",
  memory: "512",
  owner: "carol@example.com",
};

describe("RegisterSection", () => {
  it("shows nothing wrong while typing, then names each wrong field after the first Register", async () => {
    const { user } = setup();
    await fill(user, { ...VALID, slug: "Bad Slug", owner: "not-an-email" });
    expect(screen.queryByRole("alert")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Register app" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Some fields need attention",
    );
    const slug = screen.getByRole("textbox", { name: /^Slug/ });
    expect(slug).toBeInvalid();
    expect(slug).toHaveAccessibleDescription(new RegExp(SLUG_RULE));
    const owner = screen.getByRole("textbox", { name: /^Owner/ });
    expect(owner).toBeInvalid();
    expect(owner).toHaveAccessibleDescription(
      new RegExp(`Error:\\s*${EMAIL_RULE.replace(/[()]/g, "\\$&")}`),
    );
    expect(registerApp).not.toHaveBeenCalled();
  });

  it("re-checks as the person types, and clears the summary once every field is right", async () => {
    const { user } = setup();
    await fill(user, { ...VALID, slug: "Bad Slug", owner: "not-an-email" });
    await user.click(screen.getByRole("button", { name: "Register app" }));

    await user.clear(screen.getByRole("textbox", { name: /^Slug/ }));
    await user.type(
      screen.getByRole("textbox", { name: /^Slug/ }),
      "inventory",
    );
    expect(screen.getByRole("textbox", { name: /^Slug/ })).toBeValid();
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await user.clear(screen.getByRole("textbox", { name: /^Owner/ }));
    await user.type(
      screen.getByRole("textbox", { name: /^Owner/ }),
      "carol@example.com",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("refuses a slug another app already has, before any call", async () => {
    const { user } = setup();
    await fill(user, { ...VALID, slug: "sales" });
    await user.click(screen.getByRole("button", { name: "Register app" }));

    expect(
      screen.getByRole("textbox", { name: /^Slug/ }),
    ).toHaveAccessibleDescription(/slug 'sales' is already taken/);
    expect(registerApp).not.toHaveBeenCalled();
  });

  it("refuses a memory budget below the minimum, with its bound in the message", async () => {
    const { user } = setup();
    await fill(user, { ...VALID, memory: "32" });
    await user.click(screen.getByRole("button", { name: "Register app" }));

    expect(
      screen.getByRole("textbox", { name: /^Memory budget/ }),
    ).toHaveAccessibleDescription(/must be at least 64/);
    expect(registerApp).not.toHaveBeenCalled();
  });

  it("sends the contract body, then announces, refreshes and empties the form", async () => {
    vi.mocked(registerApp).mockResolvedValue(
      appDetail({ slug: "inventory", owner: "carol@example.com" }),
    );
    const { user, announce, refresh } = setup();
    await fill(user, VALID);
    await user.click(screen.getByRole("button", { name: "Register app" }));

    expect(registerApp).toHaveBeenCalledExactlyOnceWith({
      slug: "inventory",
      display_name: "Inventory",
      source_path: "/Workspace/Shared/apps/inventory",
      memory_budget_mb: 512,
      owner: "carol@example.com",
    });
    expect(await screen.findByText("Registered inventory")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Owned by carol@example.com; it appears in the list of apps.",
      ),
    ).toBeInTheDocument();
    expect(announce).toHaveBeenCalledWith("Registered inventory");
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("textbox", { name: /^Slug/ })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: /^Owner/ })).toHaveValue("");
  });

  it("is busy while the call runs and sends it only once", async () => {
    let finish: (() => void) | undefined;
    vi.mocked(registerApp).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = () => {
            resolve(appDetail({ slug: "inventory" }));
          };
        }),
    );
    const { user } = setup();
    await fill(user, VALID);
    await user.click(screen.getByRole("button", { name: "Register app" }));

    const busy = screen.getByRole("button", { name: "Registering" });
    expect(busy).toHaveAttribute("aria-busy", "true");
    await user.click(busy);
    expect(registerApp).toHaveBeenCalledTimes(1);

    finish?.();
    expect(
      await screen.findByRole("button", { name: "Register app" }),
    ).not.toHaveAttribute("aria-busy");
  });

  it("keeps what was typed and shows the server's reason when the call fails", async () => {
    vi.mocked(registerApp).mockRejectedValue(
      new ApiError(400, "invalid_request", "source folder does not exist"),
    );
    const { user, refresh } = setup();
    await fill(user, VALID);
    await user.click(screen.getByRole("button", { name: "Register app" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not register the app: The change was not accepted",
    );
    expect(alert).toHaveTextContent("source folder does not exist");
    expect(screen.getByRole("textbox", { name: /^Slug/ })).toHaveValue(
      "inventory",
    );
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Register app" }),
      ).toBeEnabled();
    });
    expect(refresh).not.toHaveBeenCalled();
  });
});
