/**
 * What the components render, checked against the strict CSP the edge serves
 * the admin UI under (FR-021, spike 19: `style-src 'self'`, `script-src 'self'`
 * plus hashes). The HTML is what `renderToStaticMarkup` writes, which is what
 * the static export prerenders; the built files are checked again in
 * tests/export. Nothing in it may hold:
 *  - a `style` attribute (the CSP refuses it);
 *  - a `<style>` element (same);
 *  - a `<script>` element (the pages' own scripts are the layout's, hashed);
 *  - an event-handler attribute (`onclick=...`) or a `javascript:` URL.
 * CSSOM writes after hydration (React's client-side `style` prop) are allowed
 * by the CSP, but the kit does not use them either (ESLint, csp-props test).
 *
 * RULE FOR A NEW SHADCN PRIMITIVE in src/components/ui:
 *  1. Give it a sample in UI_SAMPLES below. This file fails for a primitive
 *     with no sample (and for a sample with no primitive), so a primitive
 *     cannot be added without its server HTML passing the checks above.
 *  2. Do not adopt one that fails them as shipped. The Radix Checkbox, Switch,
 *     RadioGroup, Select, Tabs and ScrollArea write an inline `style` on the
 *     server; the modal ones (dialog, sheet, popover, dropdown-menu) inject a
 *     `<style>` when they open; sidebar, progress, chart and sonner do one or
 *     the other. Use a native element instead (input, select, meter, details)
 *     or write the primitive without the part that styles.
 *  3. Colours come from the tokens in globals.css, never with an opacity
 *     modifier (tests/contract/color-utilities.test.ts).
 *  4. No `style` and no `dangerouslySetInnerHTML` prop (ESLint rule, and the
 *     `@ts-expect-error` lines in csp-props.test.tsx).
 */
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { AppLink } from "@/components/app-link";
import { Button } from "@/components/button";
import { CapacityStrip } from "@/components/capacity-strip";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import { DatabricksMark } from "@/components/databricks-mark";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { LiveRegion } from "@/components/live-region";
import { JumpLinks } from "@/components/jump-links";
import { Loading } from "@/components/loading";
import { MainNav } from "@/components/main-nav";
import { Notice } from "@/components/notice";
import { PageHeader } from "@/components/page-header";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { AppStateBadge, StatusBadge } from "@/components/status-badge";
import { SubNav } from "@/components/sub-nav";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button as UiButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ChartContainer, ChartTooltipContent } from "@/components/ui/chart";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeCheckbox, NativeRadio } from "@/components/ui/native-choice";
import {
  NativeSelect,
  NativeSelectOptGroup,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import RootLayout from "@/app/layout";

const COMPONENTS_DIR = path.resolve(
  import.meta.dirname,
  "../../src/components",
);

/** What a CSP-strict page may not contain, as the reasons a piece of HTML fails. */
function cspViolations(html: string): string[] {
  const checks: readonly (readonly [reason: string, pattern: RegExp])[] = [
    ["a style attribute", /\sstyle\s*=/i],
    ["a <style> element", /<style[\s>]/i],
    ["a <script> element", /<script[\s>]/i],
    ["an event-handler attribute", /\son[a-z]+\s*=/i],
    ["a javascript: URL", /javascript:/i],
  ];
  return checks.flatMap(([reason, pattern]) =>
    pattern.test(html) ? [reason] : [],
  );
}

function assertCspClean(element: ReactElement, name: string): void {
  const html = renderToStaticMarkup(element);
  assert.ok(html.length > 0, `${name} renders something`);
  assert.deepEqual(cspViolations(html), [], `${name}: ${html}`);
}

describe("the guard itself", () => {
  it.each([
    ['<p style="color:red">x</p>', ["a style attribute"]],
    ["<style>p{}</style>", ["a <style> element"]],
    ["<script>run()</script>", ["a <script> element"]],
    ['<a onclick="go()">x</a>', ["an event-handler attribute"]],
    ['<a href="javascript:go()">x</a>', ["a javascript: URL"]],
  ])("catches %s", (html, reasons) => {
    expect(cspViolations(html)).toEqual(reasons);
  });

  it("lets ordinary markup through, including words that only look like a hit", () => {
    expect(
      cspViolations(
        '<p class="font-semibold" data-slot="x">Restyle the one-click button</p>',
      ),
    ).toEqual([]);
  });
});

// One sample per shadcn primitive, in the states that add markup.
const UI_SAMPLES: Record<string, () => ReactElement> = {
  "alert.tsx": () => (
    <Alert variant="danger">
      <AlertTitle>Title</AlertTitle>
      <AlertDescription>Description</AlertDescription>
    </Alert>
  ),
  "badge.tsx": () => (
    <>
      <Badge variant="success">Ready</Badge>
      <Badge asChild variant="info">
        <a href="/x/">Linked</a>
      </Badge>
    </>
  ),
  "button.tsx": () => (
    <>
      <UiButton variant="destructive" size="sm">
        Delete
      </UiButton>
      <UiButton asChild variant="link">
        <a href="/x/">Linked</a>
      </UiButton>
    </>
  ),
  // A chart is drawn only after the browser has taken over; the server HTML is
  // the placeholder (and the tooltip card, which is plain text).
  "chart.tsx": () => (
    <>
      <ChartContainer
        label="Requests per minute"
        description="Requests, 09:00 to 10:00."
      >
        <div />
      </ChartContainer>
      <ChartTooltipContent active label={0} tips={["09:00: 4 requests"]} />
    </>
  ),
  "empty.tsx": () => (
    <Empty>
      <EmptyHeader>
        <EmptyMedia variant="icon">!</EmptyMedia>
        <EmptyTitle>Nothing</EmptyTitle>
        <EmptyDescription>Add one.</EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <UiButton>Add</UiButton>
      </EmptyContent>
    </Empty>
  ),
  "input.tsx": () => <Input aria-invalid name="a" defaultValue="x" />,
  "label.tsx": () => <Label htmlFor="a">Label</Label>,
  "native-choice.tsx": () => (
    <>
      <NativeCheckbox name="a" defaultChecked />
      <NativeRadio name="b" value="1" />
    </>
  ),
  "native-select.tsx": () => (
    <NativeSelect name="a" defaultValue="1">
      <NativeSelectOption value="1">One</NativeSelectOption>
      <NativeSelectOptGroup label="More">
        <NativeSelectOption value="2">Two</NativeSelectOption>
      </NativeSelectOptGroup>
    </NativeSelect>
  ),
  "card.tsx": () => (
    <>
      <Card>Panel</Card>
      <Card asChild>
        <article>Article panel</article>
      </Card>
    </>
  ),
  "separator.tsx": () => (
    <>
      <Separator />
      <Separator orientation="vertical" decorative={false} />
    </>
  ),
  "skeleton.tsx": () => <Skeleton />,
  "table.tsx": () => (
    <Table frameLabel="Sample">
      <TableCaption>Caption</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead>Head</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableRow>
          <TableCell>Cell</TableCell>
        </TableRow>
      </TableBody>
      <TableFooter>
        <TableRow>
          <TableCell>Foot</TableCell>
        </TableRow>
      </TableFooter>
    </Table>
  ),
  "textarea.tsx": () => <Textarea aria-invalid name="a" defaultValue="x" />,
};

function ConfirmButtonSample() {
  return (
    <ConfirmButton
      label="Delete app"
      prompt="Delete sales?"
      confirmLabel="Yes, delete sales"
      onConfirm={() => undefined}
    />
  );
}

// One sample per component of the shared kit (src/components/*.tsx).
const KIT_SAMPLES: Record<string, () => ReactElement> = {
  "app-link.tsx": () => (
    <>
      <AppLink slug="sales">Sales</AppLink>
      <AppLink slug="sales" buttonVariant="primary" size="small">
        Open
      </AppLink>
      <AppLink slug="../etc">Rejected slug</AppLink>
    </>
  ),
  "button.tsx": () => (
    <>
      <Button variant="primary">Save</Button>
      <Button variant="danger" size="small">
        Delete
      </Button>
      <Button variant="danger-filled" size="small">
        Yes, delete
      </Button>
      <Button variant="ghost">Cancel</Button>
      <Button isBusy busyLabel="Saving">
        Save
      </Button>
    </>
  ),
  "confirm-button.tsx": () => <ConfirmButtonSample />,
  "data-table.tsx": () => (
    <>
      <DataTable
        caption="Apps"
        isCaptionHidden
        columns={[
          {
            key: "slug",
            header: "App",
            cell: (row: { slug: string; memory: number }) => row.slug,
            isRowHeader: true,
          },
          {
            key: "memory",
            header: "Memory",
            cell: (row: { slug: string; memory: number }) => row.memory,
            align: "end",
          },
        ]}
        rows={[{ slug: "sales", memory: 3 }]}
        rowKey={(row) => row.slug}
      />
      <DataTable caption="Empty" columns={[]} rows={[]} rowKey={() => ""} />
    </>
  ),
  "databricks-mark.tsx": () => <DatabricksMark />,
  "empty-state.tsx": () => (
    <EmptyState title="No apps yet" action={<Button>Register</Button>}>
      Register the first one.
    </EmptyState>
  ),
  "error-notice.tsx": () => (
    <ErrorNotice
      error={new Error("boom")}
      context="Could not save"
      onReload={() => undefined}
      onRetry={() => undefined}
    />
  ),
  "field.tsx": () => (
    <>
      <Field label="Slug" hint="Lower case." error="Taken." isRequired>
        {(control) => <Input {...control} name="slug" />}
      </Field>
      <Field label="Public" layout="inline">
        {(control) => <NativeCheckbox {...control} name="public" />}
      </Field>
    </>
  ),
  "form-layout.tsx": () => (
    <form className={FORM_STACK_CLASSES}>
      <FieldGrid>
        <Field label="Slug">{(control) => <Input {...control} />}</Field>
      </FieldGrid>
      <FormActions primary={<Button type="submit">Save</Button>}>
        <Button>Cancel</Button>
      </FormActions>
    </form>
  ),
  "jump-links.tsx": () => (
    <JumpLinks
      label="Groups"
      items={[
        { label: "Traffic limits", href: "#traffic" },
        { label: "Releases", href: "#releases" },
      ]}
    />
  ),
  "live-region.tsx": () => <LiveRegion message="Saved sales" />,
  "loading.tsx": () => <Loading label="Loading apps" />,
  "main-nav.tsx": () => <MainNav />,
  "capacity-strip.tsx": () => (
    <>
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[
          {
            key: "used",
            label: "Used",
            amount: 30,
            fill: "used",
            valueText: "30 MB",
          },
        ]}
        figure="30% used"
      />
      <CapacityStrip
        label="Memory"
        total={100}
        segments={[
          {
            key: "used",
            label: "Used",
            amount: 90,
            fill: "danger",
            valueText: "90 MB",
          },
        ]}
        level="danger"
        levelText="Almost full"
        hasLegend={false}
      />
    </>
  ),
  "notice.tsx": () => (
    <>
      <Notice tone="info" title="Heads up" />
      <Notice tone="danger" title="Failed" live="assertive">
        <p>Detail</p>
      </Notice>
    </>
  ),
  "page-header.tsx": () => (
    <PageHeader
      title="Apps"
      description="All of them."
      actions={<Button>Refresh</Button>}
    />
  ),
  "relative-time.tsx": () => (
    <>
      <RelativeTime value="2026-10-31T01:00:00Z" />
      <RelativeTime value={null} />
    </>
  ),
  "section.tsx": () => (
    <Section title="Releases" actions={<Button>Add</Button>}>
      <p>Body</p>
    </Section>
  ),
  "sub-nav.tsx": () => (
    <SubNav
      label="Sections"
      items={[
        {
          label: "Overview",
          href: "/platform/",
          badge: { text: "3", description: "3 requests waiting" },
        },
        { label: "Apps", href: "/platform/apps/" },
      ]}
    />
  ),
  "status-badge.tsx": () => (
    <>
      <AppStateBadge state="crashed" />
      <StatusBadge
        presentation={{
          label: "Ready",
          tone: "success",
          mark: "✓",
          description: "Serving.",
        }}
      />
    </>
  ),
};

function tsxFilesIn(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".tsx"))
    .map((entry) => entry.name)
    .toSorted();
}

describe("shadcn primitives (src/components/ui)", () => {
  it("every primitive has a sample here, and every sample has a primitive", () => {
    assert.deepEqual(
      tsxFilesIn(path.join(COMPONENTS_DIR, "ui")),
      Object.keys(UI_SAMPLES).toSorted(),
    );
  });

  for (const [file, sample] of Object.entries(UI_SAMPLES)) {
    it(`${file} renders no style, script or handler`, () => {
      assertCspClean(sample(), file);
    });
  }
});

describe("the shared kit (src/components)", () => {
  it("every component has a sample here, and every sample has a component", () => {
    assert.deepEqual(
      tsxFilesIn(COMPONENTS_DIR),
      Object.keys(KIT_SAMPLES).toSorted(),
    );
  });

  for (const [file, sample] of Object.entries(KIT_SAMPLES)) {
    it(`${file} renders no style, script or handler`, () => {
      assertCspClean(sample(), file);
    });
  }

  it("the question state of ConfirmButton is clean too", async () => {
    const user = userEvent.setup();
    const { container } = render(
      KIT_SAMPLES["confirm-button.tsx"]?.() ?? <></>,
    );
    await user.click(screen.getByRole("button", { name: "Delete app" }));
    assert.deepEqual(cspViolations(container.innerHTML), []);
  });

  it("the document shell is clean, with the skip link, the header and the noscript notice", () => {
    const html = renderToStaticMarkup(
      <RootLayout>
        <p>Page</p>
      </RootLayout>,
    );
    assert.ok(html.includes('href="#main"') && html.includes("<noscript>"));
    // `<noscript>` holds the markup of its Notice as text in a DOM, so the
    // scan above also covers it.
    assert.deepEqual(cspViolations(html), []);
  });
});
