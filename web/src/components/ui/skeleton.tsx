import { cn } from "cn";
import type { ComponentProps } from "react";

// shadcn new-york skeleton, on the `animate-skeleton` utility of globals.css:
// an opacity pulse in both motion preferences, slower under `reduce`.
function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-skeleton rounded-control bg-accent", className)}
      {...props}
    />
  );
}

export { Skeleton };
