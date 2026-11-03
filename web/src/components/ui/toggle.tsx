"use client";

import { cva } from "class-variance-authority";
import type { VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { Toggle as TogglePrimitive } from "radix-ui";
import type { ComponentProps } from "react";

// shadcn's toggle (new-york-v4); toggle-group takes its variants. Adapted
// like `Button`: the focus ring is the global `:focus-visible` outline (so
// `outline-none` and the `ring-ring/50` blend are gone), and an invalid toggle
// takes the destructive border without the `ring-destructive/20` blend the
// contrast test cannot see. Sizes match the kit's controls (h-10 default).
const toggleVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium whitespace-nowrap transition-colors motion-reduce:transition-none hover:bg-muted hover:text-muted-foreground disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive data-[state=on]:bg-accent data-[state=on]:text-accent-foreground [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-transparent",
        outline:
          "border border-input bg-transparent shadow-xs hover:bg-accent hover:text-accent-foreground",
      },
      size: {
        default: "h-10 min-w-10 px-2",
        sm: "h-8 min-w-8 px-1.5",
        lg: "h-11 min-w-11 px-2.5",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Toggle({
  className,
  variant,
  size,
  ...props
}: ComponentProps<typeof TogglePrimitive.Root> &
  VariantProps<typeof toggleVariants>) {
  return (
    <TogglePrimitive.Root
      data-slot="toggle"
      className={cn(toggleVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Toggle, toggleVariants };
