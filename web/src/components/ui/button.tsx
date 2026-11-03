import { cva } from "class-variance-authority";
import type { VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { Slot } from "radix-ui";
import type { ComponentProps } from "react";

// shadcn new-york button, with the hover colours as tokens of their own (the
// contrast test sees them; an opacity modifier it could not) and the focus ring
// left to the global `:focus-visible` outline in globals.css.
const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-2 rounded-control border border-transparent text-sm font-medium whitespace-nowrap no-underline transition-colors motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-60 aria-disabled:opacity-60 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:bg-primary-hover",
        destructive:
          "bg-destructive text-destructive-foreground hover:bg-destructive-hover",
        // Danger at rest is an outline, and the filled red is kept for the
        // confirm step, so only the final click is the loud one.
        "destructive-outline":
          "border-2 border-destructive bg-background text-destructive hover:bg-destructive hover:text-destructive-foreground",
        outline:
          "border-input bg-background text-foreground hover:bg-accent hover:text-accent-foreground",
        secondary: "bg-secondary text-secondary-foreground hover:bg-accent",
        ghost: "text-link hover:bg-accent hover:text-accent-foreground",
        link: "text-link underline underline-offset-4",
      },
      size: {
        default: "h-10 px-4 py-2 has-[>svg]:px-3",
        xs: "h-6 gap-1 px-2 text-xs has-[>svg]:px-1.5 [&_svg:not([class*='size-'])]:size-3",
        sm: "h-8 gap-1.5 px-3 has-[>svg]:px-2.5",
        lg: "h-11 px-6 has-[>svg]:px-4",
        icon: "size-10",
        "icon-xs": "size-6 [&_svg:not([class*='size-'])]:size-3",
        "icon-sm": "size-8",
        "icon-lg": "size-11",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot.Root : "button";

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  );
}

export { Button, buttonVariants };
