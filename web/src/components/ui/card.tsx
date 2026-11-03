import { cn } from "cn";
import { Slot } from "radix-ui";
import type { ComponentProps } from "react";

// A framed panel on the `card` token: the one place that says how a panel
// looks. It sets no layout (flex, gap) because a panel is a form, a section, an
// article or a list item, and each lays out its own content; `asChild` puts the
// frame on that element instead of wrapping it in a div.
function Card({
  className,
  asChild = false,
  ...props
}: ComponentProps<"div"> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "div";
  return (
    <Comp
      data-slot="card"
      className={cn("rounded-surface border bg-card p-4", className)}
      {...props}
    />
  );
}

export { Card };
