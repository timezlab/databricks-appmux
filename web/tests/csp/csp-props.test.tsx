import { describe, expect, it } from "vitest";

import { Button } from "@/components/button";

/**
 * The strict CSP (`style-src 'self'`) refuses `style` attributes, so the kit's
 * components must not accept the props that would carry one. The checks are the
 * `@ts-expect-error` lines: `tsc --noEmit` fails if one of them stops being an
 * error. (The shadcn primitives in src/components/ui take whatever an element
 * takes, as shadcn's do; ESLint forbids the `style` and `dangerouslySetInnerHTML`
 * attributes in every .tsx file under src, and rendered-markup.test.tsx checks what they
 * render.)
 */
describe("kit components refuse the props that would break the CSP", () => {
  it("Button has no style, className or raw-HTML prop (checked by the compiler)", () => {
    // @ts-expect-error style would render an inline style attribute
    const withStyle = <Button style={{ width: 3 }}>x</Button>;
    // @ts-expect-error classes come from variant and size only
    const withClass = <Button className="x">x</Button>;
    // @ts-expect-error raw HTML sink
    const withHtml = <Button dangerouslySetInnerHTML={{ __html: "x" }} />;
    expect([withStyle, withClass, withHtml]).toHaveLength(3);
  });
});
