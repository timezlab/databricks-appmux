"use client";

import type { VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { ToggleGroup as ToggleGroupPrimitive } from "radix-ui";
import { createContext, useContext } from "react";
import type { ComponentProps } from "react";

import { toggleVariants } from "@/components/ui/toggle";
import { useIsClient } from "@/hooks/use-is-client";

// shadcn's toggle-group (new-york-v4), adapted for the CSP in two places:
// - shadcn sets the gap with `style={{ "--gap": spacing }}`, a style
//   attribute in the server HTML. Here `spacing` picks a gap class instead,
//   so it takes the steps below rather than any number.
// - Radix's roving focus group writes `style="outline:none"` on the group, so
//   while the server prerenders and the browser hydrates (`useIsClient`) the
//   group renders without it (each item a plain toggle). Roving focus (arrow
//   keys, one tab stop) starts just after.
// shadcn's `data-[spacing=default]` never matches a number; `spacing=0` is
// what it means.
const GAP_CLASSES = {
  0: "gap-0",
  1: "gap-1",
  2: "gap-2",
  3: "gap-3",
  4: "gap-4",
} as const;

type ToggleGroupSpacing = keyof typeof GAP_CLASSES;

const ToggleGroupContext = createContext<
  VariantProps<typeof toggleVariants> & { spacing: ToggleGroupSpacing }
>({
  size: "default",
  variant: "default",
  spacing: 0,
});

function ToggleGroup({
  className,
  variant,
  size,
  spacing = 0,
  rovingFocus = true,
  children,
  ...props
}: ComponentProps<typeof ToggleGroupPrimitive.Root> &
  VariantProps<typeof toggleVariants> & {
    spacing?: ToggleGroupSpacing;
  }) {
  const isClient = useIsClient();
  return (
    <ToggleGroupPrimitive.Root
      data-slot="toggle-group"
      data-variant={variant}
      data-size={size}
      data-spacing={spacing}
      rovingFocus={isClient && rovingFocus}
      className={cn(
        "group/toggle-group flex w-fit items-center rounded-md data-[spacing=0]:data-[variant=outline]:shadow-xs",
        GAP_CLASSES[spacing],
        className,
      )}
      {...props}
    >
      <ToggleGroupContext value={{ variant, size, spacing }}>
        {children}
      </ToggleGroupContext>
    </ToggleGroupPrimitive.Root>
  );
}

function ToggleGroupItem({
  className,
  children,
  variant,
  size,
  ...props
}: ComponentProps<typeof ToggleGroupPrimitive.Item> &
  VariantProps<typeof toggleVariants>) {
  const context = useContext(ToggleGroupContext);
  const itemVariant = context.variant ?? variant;
  const itemSize = context.size ?? size;

  return (
    <ToggleGroupPrimitive.Item
      data-slot="toggle-group-item"
      data-variant={itemVariant}
      data-size={itemSize}
      data-spacing={context.spacing}
      className={cn(
        toggleVariants({ variant: itemVariant, size: itemSize }),
        "w-auto min-w-0 shrink-0 px-3 focus:z-10 focus-visible:z-10",
        "data-[spacing=0]:rounded-none data-[spacing=0]:shadow-none data-[spacing=0]:first:rounded-l-md data-[spacing=0]:last:rounded-r-md data-[spacing=0]:data-[variant=outline]:border-l-0 data-[spacing=0]:data-[variant=outline]:first:border-l",
        className,
      )}
      {...props}
    >
      {children}
    </ToggleGroupPrimitive.Item>
  );
}

export { ToggleGroup, ToggleGroupItem };
