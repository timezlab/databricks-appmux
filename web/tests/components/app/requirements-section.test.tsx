import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppDetail, ResourceTypeList } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { RequirementsSection } from "@/components/app/requirements-section";
import { readExample } from "../../support/contract-examples";

const example = AppDetail.parse(readExample("AppDetail"));
const CATALOGUE = ResourceTypeList.parse(readExample("ResourceTypeList")).items;

// One requirement still missing (with an offer), one the code names but nothing offers.
const app = AppDetail.parse({
  ...example,
  resources: [],
  requirements: {
    release_no: 8,
    items: [
      {
        key: "sql-warehouse",
        state: "missing",
        offer: {
          key: "sql-warehouse",
          type: "sql_warehouse",
          target: "a1b2c3d4e5f67890",
          permission: "CAN_USE",
        },
        suggested_type: null,
      },
      { key: "mystery", state: "missing", offer: null, suggested_type: null },
      {
        key: "experiment",
        state: "attached",
        offer: null,
        suggested_type: null,
      },
    ],
    unreferenced_offers: [
      {
        key: "spare-secret",
        type: "secret",
        target: "sales-secrets/spare",
        permission: "READ",
      },
    ],
  },
});

const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};

describe("RequirementsSection", () => {
  it("says in words which requirements are attached and which are missing", () => {
    render(
      <RequirementsSection
        app={app}
        actor={MANAGER}
        catalogue={CATALOGUE}
        onAdd={vi.fn()}
      />,
    );
    expect(screen.getByText("1 of 3 attached.")).toBeInTheDocument();

    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText("Missing")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Attached")).toBeInTheDocument();
    // An attached requirement has nothing left to add.
    expect(within(rows[2]!).queryByRole("button")).not.toBeInTheDocument();
  });

  it("names an offer's type from the server's catalogue", () => {
    render(
      <RequirementsSection
        app={app}
        actor={MANAGER}
        catalogue={CATALOGUE}
        onAdd={vi.fn()}
      />,
    );
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText("SQL warehouse")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("Can use")).toBeInTheDocument();
  });

  it("fills the Add panel with the offer, and never attaches by itself", async () => {
    const onAdd = vi.fn();
    const user = userEvent.setup();
    render(
      <RequirementsSection
        app={app}
        actor={MANAGER}
        catalogue={CATALOGUE}
        onAdd={onAdd}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add sql-warehouse" }));
    expect(onAdd).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        key: "sql-warehouse",
        type: "sql_warehouse",
        target: "a1b2c3d4e5f67890",
        permission: "CAN_USE",
        fromBundle: true,
      }),
    );
  });

  it("offers an unreferenced offer under its own heading and fills the panel from it", async () => {
    const onAdd = vi.fn();
    const user = userEvent.setup();
    render(
      <RequirementsSection
        app={app}
        actor={MANAGER}
        catalogue={CATALOGUE}
        onAdd={onAdd}
      />,
    );

    expect(
      screen.getByRole("heading", {
        name: "Offered, but nothing refers to it",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add spare-secret" }));
    expect(onAdd).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ key: "spare-secret", fromBundle: true }),
    );
  });

  it("offers no Add, and says why, to someone who cannot manage the app", () => {
    render(
      <RequirementsSection
        app={app}
        actor={{ ...MANAGER, permission: "CAN_USE", canManage: false }}
        catalogue={CATALOGUE}
        onAdd={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /^Add / }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Read only: Only an admin of this app can do this."),
    ).toBeInTheDocument();
  });
});
