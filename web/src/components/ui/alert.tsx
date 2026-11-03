import { cva } from "class-variance-authority";
import type { VariantProps } from "class-variance-authority";
import { cn } from "cn";
import type { ComponentProps } from "react";

// shadcn new-york alert, framed as the old notice was: a border in the tone's
// colour with a thick bar on the left (a shape, so the tone is not carried by
// colour alone). `default` and `destructive` are replaced by the five tones.
const alertVariants = cva(
  "relative grid w-full grid-cols-[0_1fr] items-start gap-y-1 rounded-control border border-l-4 px-4 py-3 text-sm has-[>svg]:grid-cols-[calc(var(--spacing)*4)_1fr] has-[>svg]:gap-x-3 [&_a]:text-inherit [&>svg]:size-4 [&>svg]:translate-y-0.5 [&>svg]:text-current",
  {
    variants: {
      variant: {
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
      variant: "neutral",
    },
  },
);

function Alert({
  className,
  variant,
  ...props
}: ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      data-slot="alert"
      role="alert"
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  );
}

function AlertTitle({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-title"
      className={cn("col-start-2 min-h-4 font-semibold", className)}
      {...props}
    />
  );
}

function AlertDescription({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-description"
      className={cn(
        "col-start-2 grid justify-items-start gap-2 text-sm wrap-anywhere [&_p]:leading-relaxed",
        className,
      )}
      {...props}
    />
  );
}

export { Alert, AlertTitle, AlertDescription };
