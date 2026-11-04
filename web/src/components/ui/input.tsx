import { cn } from "cn";
import type { ComponentProps } from "react";

// shadcn new-york input. The border is the `input` token (3:1 against the page
// and the card, the control-boundary rule of WCAG 1.4.11); the field is the
// card colour, so it reads as the same white well on the page and in a panel;
// focus is the global outline; invalid is a thicker destructive border, which
// the `Field` mark backs up.
function Input({ className, type, ...props }: ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-10 w-full max-w-xl min-w-0 rounded-control border border-input bg-card px-3 py-2 text-base text-foreground transition-colors selection:bg-primary selection:text-primary-foreground file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-2 aria-invalid:border-destructive",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
