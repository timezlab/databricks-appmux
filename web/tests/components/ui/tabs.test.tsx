import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

function AppSections({
  variant,
  onValueChange,
}: {
  variant?: "default" | "bar";
  onValueChange?: (value: string) => void;
}) {
  return (
    <Tabs defaultValue="overview" onValueChange={onValueChange}>
      <TabsList aria-label="Sections" variant={variant}>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="logs">Logs</TabsTrigger>
        <TabsTrigger value="environment">Environment</TabsTrigger>
      </TabsList>
      <TabsContent value="overview">Overview panel</TabsContent>
      <TabsContent value="logs">Logs panel</TabsContent>
      <TabsContent value="environment">Environment panel</TabsContent>
    </Tabs>
  );
}

function tab(name: string): HTMLElement {
  return screen.getByRole("tab", { name });
}

describe("Tabs", () => {
  it("moves between tabs with the arrow keys, Home and End, showing each one's panel", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<AppSections onValueChange={onValueChange} />);

    await user.tab();
    expect(tab("Overview")).toHaveFocus();
    expect(tab("Overview")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Overview panel");

    await user.keyboard("{ArrowRight}");
    expect(tab("Logs")).toHaveFocus();
    expect(tab("Logs")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Logs panel");
    expect(onValueChange).toHaveBeenLastCalledWith("logs");

    await user.keyboard("{End}");
    expect(tab("Environment")).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(tab("Overview")).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(tab("Environment")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(tab("Overview")).toHaveFocus();
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Overview panel");
  });

  it("keeps one tab stop: Tab leaves the list for the panel", async () => {
    const user = userEvent.setup();
    render(<AppSections />);

    await user.tab();
    await user.keyboard("{ArrowRight}");
    await user.tab();

    expect(screen.getByRole("tabpanel")).toHaveFocus();
  });

  it("marks the list with its variant, for the bar look", () => {
    render(<AppSections variant="bar" />);

    expect(screen.getByRole("tablist", { name: "Sections" })).toHaveAttribute(
      "data-variant",
      "bar",
    );
  });

  it("prerenders the tabs and the chosen panel without Radix (whose server HTML has inline styles)", () => {
    const html = renderToStaticMarkup(<AppSections variant="bar" />);
    const page = document.createElement("div");
    page.innerHTML = html;

    const tablist = page.querySelector('[role="tablist"]');
    expect(tablist).toHaveAttribute("aria-label", "Sections");
    expect(tablist).toHaveAttribute("data-variant", "bar");
    const tabs = [...page.querySelectorAll('[role="tab"]')];
    expect(
      tabs.map((element) => element.getAttribute("aria-selected")),
    ).toEqual(["true", "false", "false"]);
    expect(tabs[0]).toHaveAttribute("data-state", "active");
    const panels = [...page.querySelectorAll('[role="tabpanel"]')];
    expect(panels.map((panel) => panel.textContent)).toEqual([
      "Overview panel",
      "",
      "",
    ]);
    expect(panels[1]).toHaveAttribute("hidden");
  });
});
