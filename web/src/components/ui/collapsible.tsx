"use client";

import { Collapsible as CollapsiblePrimitive } from "radix-ui";
import type { ComponentProps } from "react";

// shadcn's collapsible (new-york-v4), as shipped: its server HTML holds no
// style, open or closed. Once open, Radix measures the content and sets its
// size variables through CSSOM writes, which the CSP allows.
function Collapsible(props: ComponentProps<typeof CollapsiblePrimitive.Root>) {
  return <CollapsiblePrimitive.Root data-slot="collapsible" {...props} />;
}

function CollapsibleTrigger(
  props: ComponentProps<typeof CollapsiblePrimitive.CollapsibleTrigger>,
) {
  return (
    <CollapsiblePrimitive.CollapsibleTrigger
      data-slot="collapsible-trigger"
      {...props}
    />
  );
}

function CollapsibleContent(
  props: ComponentProps<typeof CollapsiblePrimitive.CollapsibleContent>,
) {
  return (
    <CollapsiblePrimitive.CollapsibleContent
      data-slot="collapsible-content"
      {...props}
    />
  );
}

export { Collapsible, CollapsibleContent, CollapsibleTrigger };
