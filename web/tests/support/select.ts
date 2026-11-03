import { screen } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/**
 * Opens a `Select` (components/ui/select.tsx) by its trigger and picks the
 * option named `optionName`. The list renders in a portal, outside the
 * trigger's container, so the option is looked up on the whole screen.
 */
export async function chooseOption(
  user: UserEvent,
  trigger: HTMLElement,
  optionName: string,
): Promise<void> {
  await user.click(trigger);
  await user.click(screen.getByRole("option", { name: optionName }));
}
