import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiModule from "@/lib/api";

import { AppPanel } from "@/components/platform/app-panel";
import { ApiError, setOwner, updateApp } from "@/lib/api";
import { appDetail, platformView, renderInPlatform } from "./shell-fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  updateApp: vi.fn(),
  setOwner: vi.fn(),
  putAppEgress: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
});

const OTHER_SLUGS = ["docs", "reports"];

function setup() {
  const [sales] = platformView().apps;
  if (sales === undefined) throw new Error("The example has no app");
  const onRenamed = vi.fn();
  const onClose = vi.fn();
  const user = userEvent.setup();
  const page = renderInPlatform(
    <AppPanel
      app={sales}
      otherSlugs={OTHER_SLUGS}
      onRenamed={onRenamed}
      onClose={onClose}
    />,
  );
  return { user, onRenamed, onClose, ...page };
}

/** The platform-fields form (the one with the slug), as a set of queries. */
function fieldsForm() {
  const form = screen
    .getByRole("button", { name: "Save fields" })
    .closest("form");
  if (form === null) throw new Error("Save fields is not inside a form");
  return within(form);
}

function ownerForm() {
  const form = screen
    .getByRole("button", { name: "Change owner" })
    .closest("form");
  if (form === null) throw new Error("Change owner is not inside a form");
  return within(form);
}

describe("AppPanel", () => {
  it("opens on its heading so keyboard focus follows it, and Close calls back", async () => {
    const { user, onClose } = setup();
    expect(
      screen.getByRole("heading", { name: "sales", level: 3 }),
    ).toHaveFocus();
    expect(screen.getByRole("region", { name: "sales" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  describe("platform fields", () => {
    it("keeps Save disabled until a field differs from the app", async () => {
      const { user } = setup();
      const form = fieldsForm();
      expect(form.getByRole("button", { name: "Save fields" })).toBeDisabled();
      await user.type(form.getByRole("textbox", { name: "Display name" }), "!");
      expect(form.getByRole("button", { name: "Save fields" })).toBeEnabled();
    });

    it("sends only the changed fields with the app's version", async () => {
      vi.mocked(updateApp).mockResolvedValue(
        appDetail({ memory_budget_mb: 1024 }),
      );
      const { user, announce, refresh } = setup();
      const form = fieldsForm();
      const memory = form.getByRole("textbox", { name: "Memory budget (MB)" });
      await user.clear(memory);
      await user.type(memory, "1024");
      await user.click(form.getByRole("button", { name: "Save fields" }));

      expect(updateApp).toHaveBeenCalledExactlyOnceWith("sales", {
        version: 3,
        memory_budget_mb: 1024,
      });
      expect(await form.findByText("Saved sales")).toBeInTheDocument();
      expect(announce).toHaveBeenCalledWith("Saved sales");
      expect(refresh).toHaveBeenCalledTimes(1);
    });

    it.each([
      ["32", /must be at least 64/],
      ["99999", /must be at most 16,384/],
      ["lots", /must be a whole number of MB/],
      ["", /is required/],
    ])("refuses a memory budget of %j and says why", async (typed, message) => {
      const { user } = setup();
      const form = fieldsForm();
      const memory = form.getByRole("textbox", {
        name: "Memory budget (MB)",
      });
      await user.clear(memory);
      if (typed !== "") await user.type(memory, typed);
      await user.click(form.getByRole("button", { name: "Save fields" }));

      expect(memory).toBeInvalid();
      expect(memory).toHaveAccessibleDescription(message);
      expect(form.getByRole("alert")).toHaveTextContent(
        "Some fields need attention",
      );
      expect(updateApp).not.toHaveBeenCalled();
    });

    it("refuses a slug another app has, before any call", async () => {
      const { user } = setup();
      const form = fieldsForm();
      const slug = form.getByRole("textbox", { name: "Slug" });
      await user.clear(slug);
      await user.type(slug, "docs");
      await user.click(form.getByRole("button", { name: "Save fields" }));
      expect(slug).toHaveAccessibleDescription(/slug 'docs' is already taken/);
      expect(updateApp).not.toHaveBeenCalled();
    });

    it("hands a rename to the list, which keeps the outcome after this form is replaced", async () => {
      vi.mocked(updateApp).mockResolvedValue(appDetail({ slug: "sales-eu" }));
      const { user, onRenamed } = setup();
      const form = fieldsForm();
      const slug = form.getByRole("textbox", { name: "Slug" });
      await user.clear(slug);
      await user.type(slug, "sales-eu");
      await user.click(form.getByRole("button", { name: "Save fields" }));

      expect(updateApp).toHaveBeenCalledExactlyOnceWith("sales", {
        version: 3,
        slug: "sales-eu",
      });
      expect(onRenamed).toHaveBeenCalledExactlyOnceWith(
        "sales",
        "sales-eu",
        expect.objectContaining({
          tone: "success",
          title: "Renamed sales to sales-eu",
        }),
      );
    });
  });

  describe("owner", () => {
    it("keeps the button disabled while the owner is unchanged, then sends the version and the new owner", async () => {
      vi.mocked(setOwner).mockResolvedValue(
        appDetail({ owner: "bob@example.com" }),
      );
      const { user } = setup();
      const form = ownerForm();
      expect(form.getByRole("button", { name: "Change owner" })).toBeDisabled();

      const owner = form.getByRole("textbox", { name: "Owner" });
      await user.clear(owner);
      await user.type(owner, "bob@example.com");
      await user.click(form.getByRole("button", { name: "Change owner" }));

      expect(setOwner).toHaveBeenCalledExactlyOnceWith("sales", {
        version: 3,
        owner: "bob@example.com",
      });
      expect(
        await form.findByText("Changed the owner of sales"),
      ).toBeInTheDocument();
      expect(
        form.getByText("The owner is now bob@example.com."),
      ).toBeInTheDocument();
    });

    it("refuses an owner that is not a lower-case email", async () => {
      const { user } = setup();
      const form = ownerForm();
      const owner = form.getByRole("textbox", { name: "Owner" });
      await user.clear(owner);
      await user.type(owner, "Bob@Example.com");
      await user.click(form.getByRole("button", { name: "Change owner" }));
      expect(owner).toBeInvalid();
      expect(owner).toHaveAccessibleDescription(/must be lower case/);
      expect(setOwner).not.toHaveBeenCalled();
    });

    it("answers a stale version with the conflict message and a Reload that refetches the page", async () => {
      vi.mocked(setOwner).mockRejectedValue(
        new ApiError(
          409,
          "conflict",
          "The app was changed by someone else; reload and retry",
        ),
      );
      const { user, refresh } = setup();
      const form = ownerForm();
      const owner = form.getByRole("textbox", { name: "Owner" });
      await user.clear(owner);
      await user.type(owner, "bob@example.com");
      await user.click(form.getByRole("button", { name: "Change owner" }));

      const alert = await form.findByRole("alert");
      expect(alert).toHaveTextContent(
        "Could not change the owner of sales: Changed by someone else",
      );
      expect(alert).toHaveTextContent("The app was changed by someone else");
      expect(owner).toHaveValue("bob@example.com");

      await user.click(within(alert).getByRole("button", { name: "Reload" }));
      expect(refresh).toHaveBeenCalledTimes(1);
    });
  });
});
