import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { EgressEditor } from "@/components/platform/egress-editor";
import type { EgressEditorProps } from "@/components/platform/egress-editor";
import { PlatformProvider } from "@/components/platform/platform-context";
import { PutAppEgressRequest } from "@/lib/api";
import type { EgressSource } from "@/lib/platform/egress";

const METADATA_DENY = {
  action: "deny",
  cidr: "169.254.0.0/16",
  port: "any",
  comment: "metadata",
} as const;
const WAREHOUSE_ALLOW = {
  action: "allow",
  cidr: "10.20.0.0/16",
  port: "443",
  comment: "warehouse",
} as const;

const STORED: EgressSource = {
  rules: [METADATA_DENY, WAREHOUSE_ALLOW],
  version: 3,
};

const SAVED_TITLE = "Saved the egress rules of sales";

function setup(source: EgressSource = STORED) {
  const refresh = vi.fn();
  const announce = vi.fn();
  const save = vi.fn<EgressEditorProps["save"]>((rules, version) =>
    Promise.resolve({ rules, version: (version ?? 0) + 1 }),
  );
  const user = userEvent.setup();

  function editor(current: EgressSource): ReactNode {
    return (
      <PlatformProvider value={{ refresh, announce }}>
        <EgressEditor
          name="Egress rules of sales"
          scope="app"
          source={current}
          save={save}
          savedTitle={SAVED_TITLE}
        />
      </PlatformProvider>
    );
  }

  const view = render(editor(source));
  return {
    user,
    save,
    refresh,
    announce,
    pollAgain: (next: EgressSource) => {
      view.rerender(editor(next));
    },
  };
}

function rule(number: number) {
  return within(screen.getByRole("group", { name: `Rule ${number}` }));
}

function saveButton() {
  return screen.getByRole("button", { name: "Save rules" });
}

describe("EgressEditor", () => {
  it("refuses an allow that overlaps 169.254.0.0/16 and says so under the address field", async () => {
    const { user, save } = setup({ rules: [], version: 3 });
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.selectOptions(rule(1).getByLabelText("Action"), "allow");
    await user.type(
      rule(1).getByLabelText("Address (CIDR)"),
      "169.254.169.0/24",
    );

    // A fresh row is not red until the first Save.
    expect(rule(1).getByLabelText("Address (CIDR)")).toBeValid();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await user.click(saveButton());

    const cidr = rule(1).getByLabelText("Address (CIDR)");
    expect(cidr).toBeInvalid();
    expect(cidr).toHaveAccessibleDescription(
      /an allow cannot overlap 169\.254\.0\.0\/16/,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Some rules need attention",
    );
    expect(save).not.toHaveBeenCalled();
  });

  it("refuses a CIDR with host bits, then saves once the network address is written", async () => {
    const { user, save } = setup({ rules: [], version: 3 });
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.type(rule(1).getByLabelText("Address (CIDR)"), "10.20.0.5/16");
    await user.click(saveButton());

    expect(
      rule(1).getByLabelText("Address (CIDR)"),
    ).toHaveAccessibleDescription(/must have no host bits set/);
    expect(save).not.toHaveBeenCalled();

    const cidr = rule(1).getByLabelText("Address (CIDR)");
    await user.clear(cidr);
    await user.type(cidr, "10.20.0.0/16");
    await user.click(saveButton());

    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith(
      [{ action: "deny", cidr: "10.20.0.0/16", port: "any", comment: "" }],
      3,
    );
  });

  it("removes the chosen rule from the draft and from what is saved", async () => {
    const { user, save } = setup();
    expect(saveButton()).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Remove rule 1" }));

    expect(screen.queryByRole("group", { name: "Rule 2" })).toBeNull();
    expect(rule(1).getByLabelText("Address (CIDR)")).toHaveValue(
      "10.20.0.0/16",
    );

    await user.click(saveButton());
    expect(save).toHaveBeenCalledWith([WAREHOUSE_ALLOW], 3);
  });

  it("saves the trimmed rules in the body the schema expects, then announces the result", async () => {
    const { user, save, announce, refresh } = setup();
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.selectOptions(rule(3).getByLabelText("Action"), "allow");
    await user.type(rule(3).getByLabelText("Address (CIDR)"), " 10.30.0.0/16 ");
    const port = rule(3).getByLabelText("Port");
    await user.clear(port);
    await user.type(port, "8443");
    await user.type(rule(3).getByLabelText("Comment"), " api ");
    await user.click(saveButton());

    const [rules, version] = save.mock.calls[0] ?? [];
    expect(rules).toEqual([
      METADATA_DENY,
      WAREHOUSE_ALLOW,
      { action: "allow", cidr: "10.30.0.0/16", port: "8443", comment: "api" },
    ]);
    expect(version).toBe(3);
    expect(PutAppEgressRequest.safeParse({ version, rules }).success).toBe(
      true,
    );

    expect(await screen.findByText("3 rules stored.")).toBeInTheDocument();
    expect(announce).toHaveBeenCalledWith(SAVED_TITLE);
    expect(refresh).toHaveBeenCalledTimes(1);
    // What was saved is the new baseline: nothing left to save or discard.
    expect(saveButton()).toBeDisabled();
  });

  it("keeps the draft and blocks Save when another admin stored different rules, until Discard", async () => {
    const { user, save, pollAgain } = setup();
    await user.type(rule(2).getByLabelText("Comment"), " and more");

    const theirs: EgressSource = {
      rules: [{ ...METADATA_DENY, comment: "changed elsewhere" }],
      version: 4,
    };
    pollAgain(theirs);

    expect(
      screen.getByText("These rules changed while you were editing"),
    ).toBeInTheDocument();
    expect(rule(2).getByLabelText("Comment")).toHaveValue("warehouse and more");
    expect(saveButton()).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Discard changes" }));

    expect(
      screen.queryByText("These rules changed while you were editing"),
    ).toBeNull();
    expect(screen.queryByRole("group", { name: "Rule 2" })).toBeNull();
    expect(rule(1).getByLabelText("Comment")).toHaveValue("changed elsewhere");
    expect(save).not.toHaveBeenCalled();
  });
});
