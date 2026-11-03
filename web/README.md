# appmux admin UI

A Next.js static export that the appmux edge serves
([ADR 0012](../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md)).
No Node process runs in the slot; the build runs on a developer machine or in CI.
The stack (Tailwind, shadcn/ui, Recharts, tests outside `src/`) is recorded in
[ADR 0016](../docs/decisions/0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md).

## Commands

Run them in `web/` after `npm ci` (`.npmrc` turns install scripts off).

| Command                            | Does                                                                                                                                                                    |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `npm run build`                    | writes the export to `web/out/` (git-ignored)                                                                                                                           |
| `npm run typecheck`                | `tsc --noEmit`                                                                                                                                                          |
| `npm run lint`                     | ESLint (typescript-eslint, type-checked tiers, react-hooks, the style/HTML bans)                                                                                        |
| `npm run format:check`             | Prettier (`npm run format` writes)                                                                                                                                      |
| `npm test`                         | vitest, `unit` (Node) and `dom` (jsdom + Testing Library) projects                                                                                                      |
| `npm run test:export`              | vitest `export` project: CSS and HTML guards over `out/` (run `npm run build` first)                                                                                    |
| `npm run test:e2e`                 | Playwright: the built export in Chromium under the reference CSP (run `npm run build` first)                                                                            |
| `npm run e2e:screenshots -- <dir>` | PNGs of every page at 1280 and 390 px, light and dark, into `<dir>` outside the repo; a page that needs a click or a world of its own also gets a `-first-screen` image |
| `npm run check`                    | typecheck, lint, format:check and test                                                                                                                                  |

## Layout

```
src/app/            pages (layout.tsx owns the only <main>), globals.css (tokens); app/ and platform/ each have a layout and five sub-pages
src/components/     the kit (PageHeader, Section, Button, DataTable, Field, Notice, …)
src/components/ui/  shadcn primitives, patched for the CSP (see below)
src/components/{launcher,app,platform,dashboard}/   one folder per page
src/hooks/          React hooks (usePoll)
src/lib/api/        the API client, one file per route group, schemas/ (Zod)
src/lib/<feature>/  pure logic per page, no React
tests/              unit/ (mirrors src/lib), components/ (RTL), contract/ and csp/ (guards), export/
e2e/                the browser smoke test: server.ts (edge stand-in with the CSP and a mock API), fixtures.ts (scenarios), pages.ts, csp.spec.ts, screenshots.ts
e2e/primitives/     a page of every interactive primitive, built and prerendered by Playwright's global setup (build.ts), never part of out/
```

Tests never live in `src/`. `@/*` maps to `src/*`.

## Pages

| Route                                                                                            | Page                                                                               |
| ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| `/`                                                                                              | Apps: the launcher                                                                 |
| `/app/?slug=`, `/app/releases/`, `/app/resources/`, `/app/sharing/`, `/app/requests/`            | one app: Status, Releases, Resources, Sharing, Requests                            |
| `/platform/`, `/platform/apps/`, `/platform/access/`, `/platform/egress/`, `/platform/settings/` | the platform: Overview (inbox, memory, self-check), Apps, Access, Egress, Settings |
| `/dashboard/`                                                                                    | metrics                                                                            |

The five pages of the app and of the platform sit behind a tab bar (`SubNav`). Each area has a
server `layout.tsx` around a client shell (`AppView`, `PlatformPage`) that fetches and polls
once and hands the data to the sub-page through context, so a tab change keeps the poll and any
half-typed form. The sub-pages are thin (`app-pages.tsx`, `platform-pages.tsx`); the tab names
and badge counts are in `src/lib/app/sections.ts` and `src/lib/platform/sections.ts`. A new
sub-page is a route, an entry in `e2e/pages.ts` and a tab; the e2e run and the screenshots then
cover it.

## Writing a page

- Pages live in `src/app/**/page.tsx`; a static export cannot prerender a slug
  unknown at build time, so there are no dynamic segments (the app page reads
  `?slug=` from the query string; `appPagePath(slug)` in `src/lib/app/params.ts`
  builds that link).
- Import the API from `@/lib/api`: one function per route of
  [admin-api.md](../specs/001-appmux-v1/contracts/admin-api.md), the Zod schemas and
  their `z.infer` types, and `ApiError`. Never hand-write a type for a wire shape. Use
  `Schema.safeParse` for form input. Every call takes `{ signal }`; a poll is
  `usePoll((signal) => fn({ signal }), { intervalMs })`.
- The schemas under `src/lib/api/schemas/` must stay resolvable by plain Node (relative
  imports with a `.ts` extension, `z` only from `./zod.ts`, which sets
  `z.config({ jitless: true })`); an ESLint rule and `tests/contract/csp.test.ts` hold
  that line.
- The build sets `basePath` to `/_appmux` (`APPMUX_BASE_PATH` overrides it), so the
  files of `out/` are requested as `/_appmux/...`. The API is `/_appmux/api/v1/`.
- Request schemas are strict and carry the contract's limits and cross-field rules, so
  `safeParse` on form input refuses what the server answers `400` for. The server stays
  authoritative: rules that need its state are not checked here.
- **Page shell.** `src/app/layout.tsx` owns the only `<main>`, the skip link and the
  navigation; a page renders its content and never its own `<main>`.
- **Kit.** Build a page from the components in `src/components/` before writing markup of
  your own. `ConfirmButton` is the two-step confirmation: `window.confirm`, `alert` and
  `prompt` are banned. State is never shown by colour alone: `status-presentation.ts`
  pairs every tone with a mark.
- **Styles.** Tailwind utilities on the token colours of `src/app/globals.css`, through
  the primitives in `src/components/ui/`. Dark mode follows `prefers-color-scheme` through
  the tokens. No CSS modules, no `style` attribute or prop, no `data:` URI. Colours come
  only from tokens: no opacity modifier on a colour, no arbitrary colour value, no
  default-palette colour (`tests/contract/color-utilities.test.ts`); every token pair is
  contrast-checked (`tests/contract/contrast.test.ts`).
- **Form controls.** `Input`, `Textarea`, `Select`, `Combobox`, `NativeCheckbox`,
  `NativeRadio` inside `Field`. `Select` is the choice from a fixed list; `Combobox` adds
  search and, with `allowCustom`, a typed value. `NativeSelect` stays only where a phone's
  own picker is better; its comment lists where it is still used. The Radix checkbox, radio,
  select and dialog primitives are not used (see [Primitives](#primitives)).
- **Layout rules.** One answer for each, so a new form or table looks like the others.
  - A field is a label, the control, then its hint or error _below_ the control
    (`Field`). Fields sit in `FORM_STACK_CLASSES` or `FieldGrid` (a grid whose rows share
    the control's baseline); `FormActions` ends the form with the primary button last.
  - Every control is h-10. A `small` button (h-8) lives only in a table row, a dense list
    or a notice.
  - In any row of buttons the primary one is last: Cancel or Discard, then Save. The step
    that goes ahead in `ConfirmButton` is last too. A danger button stays outlined until
    its confirm step.
  - Spacing between things is 1, 2, 4 or 6 (`gap-1`, `gap-2`, `gap-4`, `gap-6`): 1 inside a
    cell or a line group, 2 between buttons, 4 between blocks in a section, 6 between
    sections. Table cells, tabs and buttons keep their own padding.
  - A table is a `DataTable`: stacked on a phone by default, `layout="scroll"` only for
    numeric tables that need their columns. The name of the row is the first column
    (the row header), an Actions column comes last, left-aligned, and hides its label
    when stacked (`isLabelHiddenWhenStacked`). A cell with two lines uses `gap-1`.
  - A form that saves many rows at once (Platform > Settings) has one sticky save bar at
    the bottom, not one button per row.
- **Charts.** shadcn's chart component over Recharts, patched in
  `src/components/ui/chart.tsx` to emit no runtime `<style>`; series colours are the fixed
  `--chart-1..6` tokens.
- **Links.** An `href` or `src` is a literal path on this origin, the result of
  `appLink(slug)` or `appPagePath(slug)`, or an entry of `TRUSTED_EXPRESSIONS` in
  `tests/contract/markup-rules.test.ts`. Never build one from API text. That test reads the
  syntax tree of every file under `src/` and fails on a raw HTML sink, dynamic code, a
  request outside `src/lib/api/client.ts`, a second `<main>` or an unreviewed URL
  expression. Adding to `TRUSTED_EXPRESSIONS` needs a reason in the entry.

## Primitives

The interactive shadcn primitives in `src/components/ui/` keep shadcn's names, props and
classes. Where Radix writes a `style` attribute into the server HTML or injects a `<style>`,
the primitive is adapted; each file's header says how. What floats (popover, menu, tooltip)
goes through a Radix Portal, which prerenders nothing; once open, floating-ui places it with
CSSOM writes, which the CSP allows. Colours that are opacity blends become tokens, and the
focus ring is the global `:focus-visible` outline.

| Primitive      | Built on                                       | Why it differs from shadcn                                                                                                                     |
| -------------- | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `Popover`      | Radix Popover, never modal                     | a modal one locks scrolling through react-remove-scroll, which injects a `<style>`                                                             |
| `Select`       | **Popover + Command**, shadcn's API            | Radix Select prerenders `style` attributes (its hidden native select, the value) and locks scrolling with a `<style>`                          |
| `Combobox`     | **Popover + Command** (shadcn's recipe)        | same as `Select`; adds search and, with `allowCustom`, a typed value                                                                           |
| `Command`      | cmdk, rendered only after hydration            | cmdk's screen-reader label has an inline `style`; `CommandDialog` is left out (it needs a modal Dialog)                                        |
| `DropdownMenu` | Radix, `modal={false}` fixed                   | a modal menu locks scrolling the same way                                                                                                      |
| `Tabs`         | Radix after hydration, plain ARIA markup until | Radix prerenders `style="outline:none"` on the list and `style="animation-duration:0s"` on the open panel; `variant="bar"` is the section tabs |
| `ToggleGroup`  | Radix, roving focus only after hydration       | roving focus prerenders `style="outline:none"`; `spacing` picks a gap class instead of shadcn's `--gap` style                                  |
| `Toggle`       | stock, kit sizes                               | focus ring and blend colours only                                                                                                              |
| `Tooltip`      | stock, with its own provider                   | none                                                                                                                                           |
| `Collapsible`  | stock                                          | none                                                                                                                                           |

Not added, because the CSP refuses what they write: `ScrollArea` (style attributes and a
`<style>`), Sonner (inserts a `<style>` when its module loads), and `Dialog` / `AlertDialog`
(the modal scroll lock; `ConfirmButton` is the confirmation). A new primitive needs a sample in
`tests/csp/rendered-markup.test.tsx`, which also hydrates it, and, if it opens, floats or moves
focus, a place on the e2e primitives page with a step in `driveEveryPrimitive` (`e2e/csp.spec.ts`).

## Colour and the logo

The primary colour is Databricks red. `--brand` (`#ff3621`, the fill of the official icon) paints
only non-text marks (the logo, the selected-tab bar, the focus ring); the filled buttons and link
text use darker shades of the same hue that reach 4.5:1 (`--primary`, `--link`). Red is also the
colour of danger, so a destructive control is outlined until its confirm step and its label says
what it does; never mark danger by hue alone. The header is a dark band (`--chrome`) in both
colour schemes.

The mark beside the wordmark (`src/components/databricks-mark.tsx`) is the official Databricks icon
from the press kit at databricks.com, inlined as an `<svg>` (a `data:` image would break the CSP),
unmodified, decorative.

### Trademarks

Databricks is a trademark of Databricks, Inc.; appmux is not made or endorsed by Databricks. The
mark is used only to say that appmux runs as a Databricks App.

## The CSP

The page runs under `default-src 'none'; style-src 'self'; script-src 'self'` plus the
SHA-256 of each page's inline scripts (the edge computes them from the export): no `eval`,
no `style=""` in prerendered HTML, no runtime `<style>`, no request to another origin. Four
layers hold it: the ESLint ban on `style` and `dangerouslySetInnerHTML`, the AST scan
(`tests/contract/markup-rules.test.ts`), the rendered-markup test over every primitive and
kit component (`tests/csp/`), and the built-export tests (`tests/export/`, after
`npm run build`). Adding a primitive to `src/components/ui/` needs a sample in
`tests/csp/rendered-markup.test.tsx`.

The one check that uses a real browser is `npm run test:e2e`. `e2e/server.ts` serves `out/`
under `/_appmux` with the reference policy, computing the SHA-256 of each page's inline
scripts per request, and answers the API routes with the contract examples. For each page, at
1280 and 390 px and in both colour schemes, the spec fails on a CSP violation, a console or
uncaught error, a page that draws nothing or shows a failed call, a page wider than the window,
a table squeezed below 96 px per column, a stacked table that scrolls sideways, or a visible
caption narrower than its table. A page that needs other people or other figures than the
contract examples (a visitor with a dozen apps, a nearly full slot, a failed save) sends
`X-E2E-Scenario: <name>`, and `e2e/fixtures.ts` answers the routes that scenario changes; an
unknown name throws. On the pages it observes the first load only: no polling cycle, no
interaction, no other browser.

The primitives are driven too. Playwright's global setup (`e2e/primitives/build.ts`) builds the
primitives page with Vite into a temporary folder and prerenders it; `server.ts` serves it under
`/_appmux/__e2e__/primitives/` with the same policy. At 1280 and 390 px the spec opens, types
into, picks from and closes every primitive by pointer and keyboard, and fails on a CSP
violation, a console error or an uncaught error. Five negative controls (an inline style, an
inline script outside the hash list, a missing hash, an overflow, and Radix's own Tabs and
ToggleGroup prerendered) prove the harness can fail.

Dependency vetting (npm versions, publish dates, OSV result, why the `overrides` exist) is
recorded in [research.md D2](../specs/001-appmux-v1/research.md#d2-layout-and-tooling). `cmdk`
1.1.1, added for `Command`, was vetted on 2026-11-03: the exact name shadcn's `command.tsx`
imports, published 2025-03-14, no OSV.dev advisory for any version, no install script; its
Radix dependencies dedupe with `radix-ui`.
