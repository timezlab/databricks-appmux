import { useState } from "react";

import type { RequestRow } from "@/lib/api";
import {
  answeredRequestsMessage,
  type OwnRequestsSection,
} from "@/lib/launcher/requests";

/**
 * A sentence for a screen reader when a poll shows that one of the visitor's
 * pending requests was approved or denied since the poll before it, `null`
 * otherwise. The sentence stays until the next poll brings new data.
 *
 * It compares with the previous list during render (a state kept beside the
 * props, the documented way to derive state from a change) rather than in an
 * effect, so the message and the table it describes appear in the same frame.
 */
export function useAnsweredRequests(
  section: OwnRequestsSection,
): string | null {
  const items = section.status === "ready" ? section.items : null;
  const email = section.status === "ready" ? section.email : null;

  const [previous, setPrevious] = useState<readonly RequestRow[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  if (items !== null && email !== null && items !== previous) {
    setPrevious(items);
    setMessage(answeredRequestsMessage(previous, items, email));
  }
  return message;
}
