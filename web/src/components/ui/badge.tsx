import { cva } from "class-variance-authority";
import type { VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { Slot } from "radix-ui";
import type { ComponentProps } from "react";

// shadcn new-york badge. `destructive`, `ghost` and `link` are replaced by the
// five tones of the status vocabulary (success, warning, info, neutral,
// danger): each is a text colour on its tint inside a border, and the caller
// adds the word and the mark, so colour is never the only signal.
const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-full border border-transparent px-3 py-0.5 text-xs font-semibold whitespace-nowrap [&>svg]:pointer-events-none [&>svg]:size-3",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground [a&]:hover:bg-primary-hover",
        secondary:
          "bg-secondary text-secondary-foreground [a&]:hover:bg-accent",
        outline:
          "border-input text-foreground [a&]:hover:bg-accent [a&]:hover:text-accent-foreground",
        neutral:
          "border-tone-neutral-border bg-tone-neutral-bg text-tone-neutral-text",
        info: "border-tone-info-border bg-tone-info-bg text-tone-info-text",
        success:
          "border-tone-success-border bg-tone-success-bg text-tone-success-text",
        warning:
          "border-tone-warning-border bg-tone-warning-bg text-tone-warning-text",
        danger:
          "border-tone-danger-border bg-tone-danger-bg text-tone-danger-text",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Badge({
  className,
  variant = "default",
  asChild = false,
  ...props
}: ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "span";

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}

export { Badge, badgeVariants };
