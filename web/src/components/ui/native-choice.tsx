import { cn } from "cn";
import type { ComponentProps } from "react";

// A real checkbox or radio button, drawn by the browser in the accent colour.
// shadcn's own `checkbox` and `radio-group` are Radix components whose server
// HTML carries inline styles (a hidden "bubble" input), which the CSP refuses.
const NATIVE_CHOICE_CLASSES =
  "mt-0.5 size-5 shrink-0 accent-primary disabled:cursor-not-allowed disabled:opacity-60";

function NativeCheckbox({
  className,
  ...props
}: Omit<ComponentProps<"input">, "type">) {
  return (
    <input
      type="checkbox"
      data-slot="native-checkbox"
      className={cn(NATIVE_CHOICE_CLASSES, className)}
      {...props}
    />
  );
}

function NativeRadio({
  className,
  ...props
}: Omit<ComponentProps<"input">, "type">) {
  return (
    <input
      type="radio"
      data-slot="native-radio"
      className={cn(NATIVE_CHOICE_CLASSES, className)}
      {...props}
    />
  );
}

export { NativeCheckbox, NativeRadio };
