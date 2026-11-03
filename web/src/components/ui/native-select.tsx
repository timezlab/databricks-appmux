import { cn } from "cn";
import { ChevronDownIcon } from "lucide-react";
import type { ComponentProps } from "react";

// shadcn's native-select: a real `<select>` drawn like `Input`. `className`
// goes to the select; `containerClassName` to the box around it and its
// chevron. Pick `Select` (./select.tsx, on Popover + Command, since Radix
// Select's inline styles break the CSP) for new code; keep a native one only
// where a phone's own picker is better than a popover.
//
// Still used here (as of T198), each a short fixed list the page lanes move
// to `Select`:
// - app/attach-resource-form.tsx: resource type, permission, access as
// - app/grant-form.tsx: who (the grant type), permission
// - dashboard/dashboard-controls.tsx: scope, time range
// - platform/admins-section.tsx: a new admin's type (user or group)
// - platform/egress-editor.tsx: a rule's action
// - platform/settings-section.tsx: an on/off setting, a setting's options
function NativeSelect({
  className,
  containerClassName,
  size = "default",
  ...props
}: Omit<ComponentProps<"select">, "size"> & {
  size?: "sm" | "default";
  containerClassName?: string;
}) {
  return (
    <div
      className={cn(
        "group/native-select relative w-full max-w-xl has-[select:disabled]:opacity-60",
        containerClassName,
      )}
      data-slot="native-select-wrapper"
    >
      <select
        data-slot="native-select"
        data-size={size}
        className={cn(
          "h-10 w-full min-w-0 appearance-none rounded-control border border-input bg-card px-3 py-2 pr-9 text-base text-foreground transition-colors selection:bg-primary selection:text-primary-foreground motion-reduce:transition-none disabled:cursor-not-allowed data-[size=sm]:h-8 data-[size=sm]:py-1 aria-invalid:border-2 aria-invalid:border-destructive",
          className,
        )}
        {...props}
      />
      <ChevronDownIcon
        className="pointer-events-none absolute top-1/2 right-3.5 size-4 -translate-y-1/2 text-muted-foreground select-none"
        aria-hidden="true"
        data-slot="native-select-icon"
      />
    </div>
  );
}

function NativeSelectOption({ className, ...props }: ComponentProps<"option">) {
  return (
    <option
      data-slot="native-select-option"
      className={cn("bg-[Canvas] text-[CanvasText]", className)}
      {...props}
    />
  );
}

function NativeSelectOptGroup({
  className,
  ...props
}: ComponentProps<"optgroup">) {
  return (
    <optgroup
      data-slot="native-select-optgroup"
      className={cn("bg-[Canvas] text-[CanvasText]", className)}
      {...props}
    />
  );
}

export { NativeSelect, NativeSelectOptGroup, NativeSelectOption };
