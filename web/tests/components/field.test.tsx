import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Field } from "@/components/field";
import { Input } from "@/components/ui/input";
import { NativeCheckbox } from "@/components/ui/native-choice";

describe("Field", () => {
  it("names the control by its label and describes it by hint and error", () => {
    render(
      <Field label="Slug" hint="Lower case." error="Already taken." isRequired>
        {(control) => <Input {...control} name="slug" />}
      </Field>,
    );
    const input = screen.getByRole("textbox", { name: /Slug/ });
    expect(input).toBeInvalid();
    expect(input).toBeRequired();
    expect(input).toHaveAccessibleDescription(/Lower case\./);
    expect(input).toHaveAccessibleDescription(/Error:\s*Already taken\./);
  });

  it("lists the label, then the control, then the hint, then the error", () => {
    render(
      <Field label="Slug" hint="Lower case." error="Already taken.">
        {(control) => <Input {...control} name="slug" />}
      </Field>,
    );
    const label = screen.getByText("Slug");
    const input = screen.getByRole("textbox");
    const hint = screen.getByText("Lower case.");
    const error = screen.getByText("Already taken.");
    const follows = (earlier: Element, later: Element) =>
      Boolean(
        earlier.compareDocumentPosition(later) &
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    expect(follows(label, input)).toBe(true);
    expect(follows(input, hint)).toBe(true);
    expect(follows(hint, error)).toBe(true);
  });

  it("marks nothing invalid and describes nothing when there is no hint or error", () => {
    render(<Field label="Name">{(control) => <Input {...control} />}</Field>);
    const input = screen.getByRole("textbox", { name: "Name" });
    expect(input).not.toHaveAttribute("aria-invalid");
    expect(input).not.toHaveAttribute("aria-describedby");
  });

  it("puts the control before the label in the inline layout, still tied to it", () => {
    render(
      <Field label="Public" layout="inline" hint="Anyone can open it.">
        {(control) => <NativeCheckbox {...control} name="public" />}
      </Field>,
    );
    const checkbox = screen.getByRole("checkbox", { name: "Public" });
    const label = screen.getByText("Public");
    expect(
      checkbox.compareDocumentPosition(label) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(checkbox).toHaveAccessibleDescription("Anyone can open it.");
  });
});
