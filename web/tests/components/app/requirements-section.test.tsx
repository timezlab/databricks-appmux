import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppDetail } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { RequirementsSection } from "@/components/app/requirements-section";
import { readExample } from "../../support/contract-examples";

const example = AppDetail.parse(readExample("AppDetail"));

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
      <RequirementsSection app={app} actor={MANAGER} onAttach={vi.fn()} />,
    );
    expect(screen.getByText("1 of 3 attached.")).toBeInTheDocument();

    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText(/Missing/)).toBeInTheDocument();
    expect(within(rows[2]!).getByText(/Attached/)).toBeInTheDocument();
    // An attached requirement has nothing left to attach.
    expect(within(rows[2]!).queryByRole("button")).not.toBeInTheDocument();
  });

  it("fills the attach form with the offer, and never attaches by itself", async () => {
    const onAttach = vi.fn();
    const user = userEvent.setup();
    render(
      <RequirementsSection app={app} actor={MANAGER} onAttach={onAttach} />,
    );

    await user.click(
      screen.getByRole("button", { name: "Attach sql-warehouse" }),
    );
    expect(onAttach).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        key: "sql-warehouse",
        type: "sql_warehouse",
        target: "a1b2c3d4e5f67890",
        permission: "CAN_USE",
        fromBundle: true,
      }),
    );
  });

  it("offers an unreferenced offer under its own heading and fills the form from it", async () => {
    const onAttach = vi.fn();
    const user = userEvent.setup();
    render(
      <RequirementsSection app={app} actor={MANAGER} onAttach={onAttach} />,
    );

    expect(
      screen.getByRole("heading", {
        name: "Offered, but nothing refers to it",
      }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Attach spare-secret" }),
    );
    expect(onAttach).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ key: "spare-secret", fromBundle: true }),
    );
  });

  it("turns every Attach off, and says why, for someone who cannot manage the app", () => {
    render(
      <RequirementsSection
        app={app}
        actor={{ ...MANAGER, permission: "CAN_USE", canManage: false }}
        onAttach={vi.fn()}
      />,
    );
    for (const name of [
      "Attach sql-warehouse",
      "Attach mystery",
      "Attach spare-secret",
    ]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(
      screen.getByText("Attach: Only an admin of this app can do this."),
    ).toBeInTheDocument();
  });
});
