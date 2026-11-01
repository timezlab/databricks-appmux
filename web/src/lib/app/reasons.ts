/**
 * Why buttons are off, in words. A disabled button shows no tooltip in every
 * browser and says nothing to a keyboard user, so the reason is written next
 * to the buttons instead.
 */
import type { Availability } from "./actions";

export interface LabelledAvailability {
  /** The button's name ("Delete"). */
  label: string;
  availability: Availability;
}

function joinLabels(labels: readonly string[]): string {
  if (labels.length <= 1) return labels.join("");
  const last = labels[labels.length - 1] ?? "";
  return `${labels.slice(0, -1).join(", ")} and ${last}`;
}

/** One line per distinct reason, in the order the actions were given; none when every action is on. */
export function unavailableReasons(
  entries: readonly LabelledAvailability[],
): string[] {
  const labelsByReason = new Map<string, string[]>();
  for (const { label, availability } of entries) {
    if (availability.isAllowed) continue;
    const labels = labelsByReason.get(availability.reason) ?? [];
    labels.push(label);
    labelsByReason.set(availability.reason, labels);
  }
  return [...labelsByReason].map(
    ([reason, labels]) => `${joinLabels(labels)}: ${reason}`,
  );
}
