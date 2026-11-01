import { cn } from "cn";
import type { ComponentProps } from "react";

// shadcn new-york textarea. A fixed minimum height that the person can pull
// taller, instead of `field-sizing: content` (not in every supported browser).
function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex min-h-24 w-full max-w-xl resize-y rounded-md border border-input bg-background px-3 py-2 text-base text-foreground transition-colors placeholder:text-muted-foreground motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-2 aria-invalid:border-destructive",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
