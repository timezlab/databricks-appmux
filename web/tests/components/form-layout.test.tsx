import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "@/components/button";
import { Field } from "@/components/field";
import { FieldGrid, FormActions } from "@/components/form-layout";
import { Input } from "@/components/ui/input";

describe("FormActions", () => {
  it("puts the primary button last, after the secondary ones, whatever the order it was written in", () => {
    render(
      <FormActions
        primary={
          <Button type="submit" variant="primary">
            Save rules
          </Button>
        }
      >
        <Button>Add rule</Button>
        <Button variant="ghost">Discard changes</Button>
      </FormActions>,
    );
    const names = screen
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(names).toEqual(["Add rule", "Discard changes", "Save rules"]);
  });

  it("works with only a primary button", () => {
    render(<FormActions primary={<Button>Register app</Button>} />);
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });
});

describe("FieldGrid", () => {
  it("holds its fields as direct children so they share one row baseline", () => {
    render(
      <FieldGrid>
        <Field label="Slug" hint="Lower case.">
          {(control) => <Input {...control} />}
        </Field>
        <Field label="Name">{(control) => <Input {...control} />}</Field>
      </FieldGrid>,
    );
    const grid = screen
      .getByRole("textbox", { name: "Slug" })
      .closest("div.grid");
    expect(grid).not.toBeNull();
    expect(within(grid as HTMLElement).getAllByRole("textbox")).toHaveLength(2);
  });
});
