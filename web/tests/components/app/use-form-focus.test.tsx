import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import type { FormEvent } from "react";
import { describe, expect, it } from "vitest";

import { useFocusFirstInvalid } from "@/components/app/use-form-focus";
import { Field } from "@/components/field";
import { Input } from "@/components/ui/input";

/** A form whose Send always fails the checks: `badFields` are the ones marked invalid. */
function Form({ badFields }: { badFields: readonly ("first" | "second")[] }) {
  const [errors, setErrors] = useState<readonly string[]>([]);
  const { formRef, reportInvalid } = useFocusFirstInvalid();

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setErrors(badFields);
    reportInvalid();
  }

  return (
    <form ref={formRef} onSubmit={handleSubmit} noValidate aria-label="Test">
      <Field label="First" error={errors.includes("first") ? "Wrong." : null}>
        {(control) => <Input {...control} />}
      </Field>
      <Field label="Second" error={errors.includes("second") ? "Wrong." : null}>
        {(control) => <Input {...control} />}
      </Field>
      <button type="submit">Send</button>
    </form>
  );
}

describe("useFocusFirstInvalid", () => {
  it("moves the keyboard to the first invalid control after a failed send", async () => {
    const user = userEvent.setup();
    render(<Form badFields={["first", "second"]} />);

    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByRole("textbox", { name: "First" })).toHaveFocus();
  });

  it("skips the controls that are fine", async () => {
    const user = userEvent.setup();
    render(<Form badFields={["second"]} />);

    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByRole("textbox", { name: "Second" })).toHaveFocus();
  });

  it("does it again on a second failed send with the very same errors", async () => {
    const user = userEvent.setup();
    render(<Form badFields={["second"]} />);
    await user.click(screen.getByRole("button", { name: "Send" }));
    await user.click(screen.getByRole("textbox", { name: "First" }));
    expect(screen.getByRole("textbox", { name: "First" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByRole("textbox", { name: "Second" })).toHaveFocus();
  });

  it("leaves the keyboard where it is until a send fails", () => {
    render(<Form badFields={["first"]} />);
    expect(document.body).toHaveFocus();
  });
});
