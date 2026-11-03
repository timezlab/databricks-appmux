import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { Combobox, type ComboboxOption } from "@/components/ui/combobox";

const PEOPLE: readonly ComboboxOption[] = [
  { value: "data-team", label: "Data team", description: "Group" },
  { value: "ada@example.com", label: "Ada", description: "User" },
  { value: "grace@example.com", label: "Grace", description: "User" },
];

interface PrincipalFieldProps {
  options?: readonly ComboboxOption[];
  initialValue?: string;
  onValueChange?: (value: string, option: ComboboxOption | null) => void;
  onSearchChange?: (search: string) => void;
  loading?: boolean;
  allowCustom?: boolean;
  renderOption?: (option: ComboboxOption) => ReactNode;
}

function PrincipalField({
  options = PEOPLE,
  initialValue = "",
  onValueChange,
  ...rest
}: PrincipalFieldProps) {
  const [value, setValue] = useState(initialValue);
  return (
    <>
      <label htmlFor="principal">Principal</label>
      <Combobox
        id="principal"
        options={options}
        value={value}
        onValueChange={(next, option) => {
          setValue(next);
          onValueChange?.(next, option);
        }}
        placeholder="Choose a principal"
        searchPlaceholder="Search people and groups"
        emptyMessage="Nobody matches."
        {...rest}
      />
    </>
  );
}

function trigger(): HTMLElement {
  return screen.getByRole("combobox", { name: "Principal" });
}

function search(): HTMLElement {
  return screen.getByRole("combobox", { name: "Search people and groups" });
}

function optionLabels(): string[] {
  return within(screen.getByRole("listbox"))
    .queryAllByRole("option")
    .map((option) => option.textContent);
}

describe("Combobox (Popover + Command)", () => {
  it("shows the placeholder, then the chosen option's label", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<PrincipalField onValueChange={onValueChange} />);

    expect(trigger()).toHaveTextContent("Choose a principal");
    expect(trigger()).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger());
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    expect(search()).toHaveFocus();
    await user.click(screen.getByRole("option", { name: /Grace/ }));

    expect(onValueChange).toHaveBeenCalledExactlyOnceWith(
      "grace@example.com",
      PEOPLE[2],
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger()).toHaveTextContent("Grace");
    expect(trigger()).toHaveFocus();
  });

  it("filters the options itself when there is no server search, and says when nothing matches", async () => {
    const user = userEvent.setup();
    render(<PrincipalField />);

    await user.click(trigger());
    await user.keyboard("gra");
    expect(optionLabels()).toEqual([expect.stringContaining("Grace")]);

    await user.keyboard("zzz");
    expect(screen.getByText("Nobody matches.")).toBeInTheDocument();
    expect(optionLabels()).toEqual([]);
  });

  it("hands each search to onSearchChange and shows the options it is given unfiltered", async () => {
    const user = userEvent.setup();
    const onSearchChange = vi.fn();
    render(<PrincipalField onSearchChange={onSearchChange} />);

    await user.click(trigger());
    await user.keyboard("ad");

    expect(onSearchChange.mock.calls).toEqual([["a"], ["ad"]]);
    // The server decides what matches: the list is what the caller passes.
    expect(optionLabels()).toHaveLength(PEOPLE.length);
  });

  it("shows the loading state instead of the empty message", async () => {
    const user = userEvent.setup();
    render(
      <PrincipalField options={[]} onSearchChange={() => undefined} loading />,
    );

    await user.click(trigger());
    await user.keyboard("x");

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Nobody matches.")).not.toBeInTheDocument();
  });

  it("picks with the keyboard and closes on Escape with focus back on the trigger", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<PrincipalField onValueChange={onValueChange} />);

    trigger().focus();
    await user.keyboard("{Enter}");
    expect(search()).toHaveFocus();
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onValueChange).toHaveBeenCalledExactlyOnceWith(
      "ada@example.com",
      PEOPLE[1],
    );

    await user.keyboard("{Enter}");
    expect(search()).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(trigger()).toHaveTextContent("Ada");
  });

  it("starts each opening with an empty search", async () => {
    const user = userEvent.setup();
    const onSearchChange = vi.fn();
    render(<PrincipalField onSearchChange={onSearchChange} />);

    await user.click(trigger());
    await user.keyboard("gr{Escape}");
    await user.click(trigger());

    expect(search()).toHaveValue("");
    expect(onSearchChange).toHaveBeenLastCalledWith("");
  });

  it("accepts what was typed as the value when allowCustom is set", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(
      <PrincipalField options={[]} allowCustom onValueChange={onValueChange} />,
    );

    await user.click(trigger());
    await user.keyboard("  someone@example.com ");
    const custom = screen.getByRole("option", {
      name: "Use “someone@example.com”",
    });
    expect(custom).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");

    expect(onValueChange).toHaveBeenCalledExactlyOnceWith(
      "someone@example.com",
      null,
    );
    expect(trigger()).toHaveTextContent("someone@example.com");
  });

  it("offers no custom row for an empty search or one that is already an option", async () => {
    const user = userEvent.setup();
    render(<PrincipalField allowCustom />);

    await user.click(trigger());
    expect(screen.queryByRole("option", { name: /^Use / })).toBeNull();
    await user.keyboard("data-team");
    expect(screen.queryByRole("option", { name: /^Use / })).toBeNull();
  });

  it("draws each row with renderOption", async () => {
    const user = userEvent.setup();
    render(
      <PrincipalField
        renderOption={(option) => (
          <span>
            {option.label} ({option.description})
          </span>
        )}
      />,
    );

    await user.click(trigger());

    expect(
      screen.getByRole("option", { name: "Data team (Group)" }),
    ).toBeInTheDocument();
  });
});
