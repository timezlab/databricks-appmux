"use client";

import { cva } from "class-variance-authority";
import type { VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { Slot, Tabs as TabsPrimitive } from "radix-ui";
import { createContext, useContext, useId } from "react";
import type { ComponentProps } from "react";

import { useIsClient } from "@/hooks/use-is-client";

// shadcn's tabs (new-york-v4), adapted for the CSP. Radix's list (a roving
// focus group) writes `style="outline:none"` and the open panel
// `style="animation-duration:0s"` into the server HTML, which the CSP
// (style-src 'self') refuses. So while the server prerenders and the browser
// hydrates (`useIsClient`), the list, tabs and panels render the same ARIA
// markup by hand, with the chosen tab's panel; Radix takes over just after,
// with its keyboard (arrows, Home, End) and focus handling.
// Colours: shadcn's tab text (foreground at 60 %) and dark-mode fill (input at
// 30 %) are opacity blends the contrast test cannot see, so they are
// `text-muted-foreground` and gone; the focus ring is the global
// `:focus-visible` outline of globals.css.
//
// `variant="bar"` is the bar of the section tabs. Its triggers carry the shared
// `bar-item` geometry of globals.css (a 2 px bar, `primary` on the chosen tab,
// no text underline), the same as the top navigation, the sidebar and
// `SubNav`; they do not take the pill classes of the other variants.

interface TabsStaticContextValue {
  baseId: string;
  value: string | undefined;
  orientation: "horizontal" | "vertical";
}

const TabsStaticContext = createContext<TabsStaticContextValue | null>(null);

// The list's variant, for the triggers inside it: the bar variant swaps the whole
// class set, which a `group-data-` selector cannot do without fighting the pill classes.
type TabsListVariant = NonNullable<
  VariantProps<typeof tabsListVariants>["variant"]
>;
const TabsListVariantContext = createContext<TabsListVariant>("default");

function useTabsStatic(part: string): TabsStaticContextValue {
  const context = useContext(TabsStaticContext);
  if (context === null) {
    throw new Error(`<${part}> must be inside <Tabs>.`);
  }
  return context;
}

function staticIds(baseId: string, value: string) {
  return {
    trigger: `${baseId}-trigger-${value}`,
    content: `${baseId}-content-${value}`,
  };
}

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: ComponentProps<typeof TabsPrimitive.Root>) {
  const baseId = useId();
  return (
    <TabsStaticContext
      value={{
        baseId,
        value: props.value ?? props.defaultValue,
        orientation,
      }}
    >
      <TabsPrimitive.Root
        data-slot="tabs"
        data-orientation={orientation}
        orientation={orientation}
        className={cn(
          "group/tabs flex gap-2 data-[orientation=horizontal]:flex-col",
          className,
        )}
        {...props}
      />
    </TabsStaticContext>
  );
}

const tabsListVariants = cva(
  "group/tabs-list inline-flex w-fit items-center justify-center rounded-lg p-[3px] text-muted-foreground group-data-[orientation=horizontal]/tabs:h-9 group-data-[orientation=vertical]/tabs:h-fit group-data-[orientation=vertical]/tabs:flex-col data-[variant=line]:rounded-none",
  {
    variants: {
      variant: {
        default: "bg-muted",
        line: "gap-1 bg-transparent",
        bar: "w-full flex-wrap justify-start gap-x-1 rounded-none border-b bg-transparent p-0 group-data-[orientation=horizontal]/tabs:h-auto",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function TabsList({
  className,
  variant = "default",
  ...props
}: ComponentProps<typeof TabsPrimitive.List> &
  VariantProps<typeof tabsListVariants>) {
  const isClient = useIsClient();
  const tabs = useTabsStatic("TabsList");
  const shared = {
    "data-slot": "tabs-list",
    "data-variant": variant ?? "default",
    className: cn(tabsListVariants({ variant }), className),
  };
  if (isClient) {
    return (
      <TabsListVariantContext value={variant ?? "default"}>
        <TabsPrimitive.List {...shared} {...props} />
      </TabsListVariantContext>
    );
  }
  const { asChild, ...listProps } = props;
  delete listProps.loop; // Radix's option, not an attribute
  const Comp = asChild === true ? Slot.Root : "div";
  return (
    <TabsListVariantContext value={variant ?? "default"}>
      <Comp
        role="tablist"
        aria-orientation={tabs.orientation}
        {...shared}
        {...listProps}
      />
    </TabsListVariantContext>
  );
}

const TABS_TRIGGER_CLASSES = [
  "relative inline-flex h-[calc(100%-1px)] flex-1 items-center justify-center gap-1.5 rounded-md border border-transparent px-2 py-1 text-sm font-medium whitespace-nowrap text-muted-foreground transition-all motion-reduce:transition-none group-data-[orientation=vertical]/tabs:w-full group-data-[orientation=vertical]/tabs:justify-start hover:text-foreground disabled:pointer-events-none disabled:opacity-50 group-data-[variant=default]/tabs-list:data-[state=active]:shadow-sm group-data-[variant=line]/tabs-list:data-[state=active]:shadow-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  "group-data-[variant=line]/tabs-list:bg-transparent group-data-[variant=line]/tabs-list:data-[state=active]:bg-transparent dark:group-data-[variant=line]/tabs-list:data-[state=active]:border-transparent dark:group-data-[variant=line]/tabs-list:data-[state=active]:bg-transparent",
  "data-[state=active]:bg-background data-[state=active]:text-foreground dark:data-[state=active]:border-input dark:data-[state=active]:text-foreground",
  "after:absolute after:bg-foreground after:opacity-0 after:transition-opacity group-data-[orientation=horizontal]/tabs:after:inset-x-0 group-data-[orientation=horizontal]/tabs:after:bottom-[-5px] group-data-[orientation=horizontal]/tabs:after:h-0.5 group-data-[orientation=vertical]/tabs:after:inset-y-0 group-data-[orientation=vertical]/tabs:after:-right-1 group-data-[orientation=vertical]/tabs:after:w-0.5 group-data-[variant=line]/tabs-list:data-[state=active]:after:opacity-100",
];

// The bar variant's trigger: `bar-item` draws the bar; `-mb-px` lays it over the
// list's hairline, as `SubNav` does.
const TABS_BAR_TRIGGER_CLASSES =
  "bar-item -mb-px whitespace-nowrap disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4";

function TabsTrigger({
  className,
  ...props
}: ComponentProps<typeof TabsPrimitive.Trigger>) {
  const isClient = useIsClient();
  const tabs = useTabsStatic("TabsTrigger");
  const variant = useContext(TabsListVariantContext);
  const classes = cn(
    variant === "bar" ? TABS_BAR_TRIGGER_CLASSES : TABS_TRIGGER_CLASSES,
    className,
  );
  if (isClient) {
    return (
      <TabsPrimitive.Trigger
        data-slot="tabs-trigger"
        className={classes}
        {...props}
      />
    );
  }
  const { value, disabled, asChild, ...buttonProps } = props;
  const isActive = value === tabs.value;
  const ids = staticIds(tabs.baseId, value);
  const Comp = asChild === true ? Slot.Root : "button";
  return (
    <Comp
      type="button"
      role="tab"
      id={ids.trigger}
      aria-selected={isActive}
      aria-controls={ids.content}
      data-state={isActive ? "active" : "inactive"}
      data-disabled={disabled === true ? "" : undefined}
      data-orientation={tabs.orientation}
      disabled={disabled}
      tabIndex={isActive ? 0 : -1}
      data-slot="tabs-trigger"
      className={classes}
      {...buttonProps}
    />
  );
}

function TabsContent({
  className,
  ...props
}: ComponentProps<typeof TabsPrimitive.Content>) {
  const isClient = useIsClient();
  const tabs = useTabsStatic("TabsContent");
  const classes = cn("flex-1", className);
  if (isClient) {
    return (
      <TabsPrimitive.Content
        data-slot="tabs-content"
        className={classes}
        {...props}
      />
    );
  }
  const { value, forceMount, asChild, children, ...panelProps } = props;
  const isActive = value === tabs.value;
  const ids = staticIds(tabs.baseId, value);
  const Comp = asChild === true ? Slot.Root : "div";
  return (
    <Comp
      role="tabpanel"
      id={ids.content}
      aria-labelledby={ids.trigger}
      data-state={isActive ? "active" : "inactive"}
      data-orientation={tabs.orientation}
      hidden={!isActive}
      tabIndex={0}
      data-slot="tabs-content"
      className={classes}
      {...panelProps}
    >
      {isActive || forceMount === true ? children : null}
    </Comp>
  );
}

export { Tabs, TabsContent, TabsList, TabsTrigger, tabsListVariants };
