import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface FruitSelectProps {
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  searchPlaceholder?: string;
  disabled?: boolean;
}

function FruitSelect({
  defaultValue,
  onValueChange,
  searchPlaceholder,
  disabled,
}: FruitSelectProps) {
  return (
    <form aria-label="Order">
      <label htmlFor="fruit">Fruit</label>
      <Select
        name="fruit"
        defaultValue={defaultValue}
        onValueChange={onValueChange}
        disabled={disabled}
      >
        <SelectTrigger id="fruit">
          <SelectValue placeholder="Pick a fruit" />
        </SelectTrigger>
        <SelectContent
          searchPlaceholder={searchPlaceholder}
          emptyMessage="No fruit matches."
        >
          <SelectGroup>
            <SelectLabel>Fruit</SelectLabel>
            <SelectItem value="apple">Apple</SelectItem>
            <SelectItem value="banana">Banana</SelectItem>
            <SelectItem value="blueberry">Blueberry</SelectItem>
            <SelectItem value="cherry" disabled>
              Cherry
            </SelectItem>
            <SelectItem value="date">Date</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>
      <button type="button">After</button>
    </form>
  );
}

function trigger(): HTMLElement {
  return screen.getByRole("combobox", { name: "Fruit" });
}

function hiddenInput(): HTMLInputElement {
  const form = screen.getByRole("form", { name: "Order" });
  const input = form.querySelector<HTMLInputElement>('input[name="fruit"]');
  if (input === null) throw new Error("no hidden input named fruit");
  return input;
}

function highlightedOption(): HTMLElement {
  const highlighted = within(screen.getByRole("listbox"))
    .getAllByRole("option")
    .filter((option) => option.getAttribute("aria-selected") === "true");
  expect(highlighted).toHaveLength(1);
  const [only] = highlighted;
  if (only === undefined) throw new Error("no option is highlighted");
  return only;
}

describe("Select (Popover + Command)", () => {
  it("is a combobox that announces a listbox and shows the chosen item's label", () => {
    render(<FruitSelect defaultValue="banana" />);

    expect(trigger()).toHaveAttribute("aria-haspopup", "listbox");
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    expect(trigger()).toHaveTextContent("Banana");
    expect(trigger()).not.toHaveAttribute("data-placeholder");
    expect(hiddenInput()).toHaveValue("banana");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("shows the placeholder while nothing is chosen", () => {
    render(<FruitSelect />);

    expect(trigger()).toHaveTextContent("Pick a fruit");
    expect(trigger()).toHaveAttribute("data-placeholder");
    expect(hiddenInput()).toHaveValue("");
  });

  it("opens with the keyboard on the chosen item, moves with arrows, Home and End, and picks with Enter", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<FruitSelect defaultValue="banana" onValueChange={onValueChange} />);

    await user.tab();
    expect(trigger()).toHaveFocus();
    await user.keyboard("{ArrowDown}");

    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    const listbox = screen.getByRole("listbox");
    expect(listbox).toContainElement(document.activeElement as HTMLElement);
    expect(highlightedOption()).toHaveTextContent("Banana");

    await user.keyboard("{ArrowDown}");
    expect(highlightedOption()).toHaveTextContent("Blueberry");
    // Cherry is disabled, so the next stop is Date.
    await user.keyboard("{ArrowDown}");
    expect(highlightedOption()).toHaveTextContent("Date");
    await user.keyboard("{Home}");
    expect(highlightedOption()).toHaveTextContent("Apple");
    await user.keyboard("{End}");
    expect(highlightedOption()).toHaveTextContent("Date");

    await user.keyboard("{Enter}");

    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("date");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(trigger()).toHaveTextContent("Date");
    expect(hiddenInput()).toHaveValue("date");
  });

  it("opens with Enter and Space, and Space picks the highlighted item", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<FruitSelect onValueChange={onValueChange} />);

    trigger().focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    // With nothing chosen the first item is highlighted.
    expect(highlightedOption()).toHaveTextContent("Apple");
    await user.keyboard("{Escape}");

    await user.keyboard(" ");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{ArrowDown} ");

    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("banana");
    expect(trigger()).toHaveTextContent("Banana");
  });

  it("jumps to an item by typing its first letters (type-ahead)", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<FruitSelect defaultValue="apple" onValueChange={onValueChange} />);

    trigger().focus();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("bl");
    expect(highlightedOption()).toHaveTextContent("Blueberry");
    await user.keyboard("{Enter}");

    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("blueberry");
  });

  it("filters by typing in its search box when it has one", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(
      <FruitSelect
        searchPlaceholder="Search fruit"
        onValueChange={onValueChange}
      />,
    );

    await user.click(trigger());
    const search = screen.getByRole("combobox", { name: "Search fruit" });
    expect(search).toHaveFocus();

    await user.keyboard("blue");
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual(["Blueberry"]);

    await user.keyboard("{Enter}");
    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("blueberry");
    expect(trigger()).toHaveFocus();
  });

  it("says so when the search matches nothing", async () => {
    const user = userEvent.setup();
    render(<FruitSelect searchPlaceholder="Search fruit" />);

    await user.click(trigger());
    await user.keyboard("kiwi");

    expect(screen.getByText("No fruit matches.")).toBeInTheDocument();
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("closes on Escape without changing the value and gives focus back to the trigger", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<FruitSelect defaultValue="banana" onValueChange={onValueChange} />);

    await user.click(trigger());
    await user.keyboard("{ArrowDown}{Escape}");

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(trigger()).toHaveTextContent("Banana");
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it("closes on Tab and keeps the value", async () => {
    const user = userEvent.setup();
    render(<FruitSelect defaultValue="banana" />);

    await user.click(trigger());
    await user.keyboard("{ArrowDown}");
    await user.tab();

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger()).toHaveTextContent("Banana");
  });

  it("picks an item on click, but not a disabled one", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<FruitSelect onValueChange={onValueChange} />);

    await user.click(trigger());
    await user.click(screen.getByRole("option", { name: "Cherry" }));
    expect(onValueChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    await user.click(screen.getByRole("option", { name: "Apple" }));
    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("apple");
    expect(trigger()).toHaveTextContent("Apple");
  });

  it("does not open when disabled", async () => {
    const user = userEvent.setup();
    render(<FruitSelect disabled />);

    expect(trigger()).toBeDisabled();
    await user.click(trigger());
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
