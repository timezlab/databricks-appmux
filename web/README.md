# appmux admin UI

A Next.js static export that the appmux edge serves
([ADR 0012](../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md)).
No Node process runs in the slot; the build runs on a developer machine or in CI.
The stack (Tailwind, shadcn/ui, Recharts, tests outside `src/`) is recorded in
[ADR 0016](../docs/decisions/0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md).

## Commands

Run them in `web/` after `npm ci` (`.npmrc` turns install scripts off).

| Command                                                  | Does                                                                                                                                                                                                                                            |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `npm run build`                                          | writes the export to `web/out/` (git-ignored)                                                                                                                                                                                                   |
| `npm run typecheck`                                      | `tsc --noEmit`                                                                                                                                                                                                                                  |
| `npm run lint`                                           | ESLint (typescript-eslint, type-checked tiers, react-hooks, the style/HTML bans)                                                                                                                                                                |
| `npm run format:check`                                   | Prettier (`npm run format` writes)                                                                                                                                                                                                              |
| `npm test`                                               | vitest, `unit` (Node) and `dom` (jsdom + Testing Library) projects                                                                                                                                                                              |
| `npm run test:export`                                    | vitest `export` project: CSS and HTML guards over `out/` (run `npm run build` first)                                                                                                                                                            |
| `npm run test:e2e`                                       | Playwright: the built export in Chromium under the reference CSP (run `npm run build` first)                                                                                                                                                    |
| `npm run e2e:screenshots -- <dir> [light\|dark\|system]` | PNGs of every page at 1280 and 390 px, OS light and dark, into `<dir>` outside the repo; the optional theme is stored as the choice first and ends each name; a page that needs a click or a world of its own also gets a `-first-screen` image |
| `npm run check`                                          | typecheck, lint, format:check and test                                                                                                                                                                                                          |

## Layout

```
src/app/            pages (layout.tsx owns the only <main>), globals.css (tokens, radius scale, the bar, motion), fonts.css (IBM Plex, self-hosted); app/ has a layout and ten sections, platform/ a layout and five sub-pages
src/components/     the kit (PageHeader, Section, SectionPanel, AppSidebar, RouteStrip, Lamp, skeletons, CopyButton, Button, DataTable, Field, Notice, …)
src/components/ui/  shadcn primitives, patched for the CSP (see below)
src/components/{launcher,app,platform,dashboard}/   one folder per page
src/hooks/          React hooks (usePoll, useStoredFlag, useThemeChoice, useAppDisplayName)
src/lib/api/        the API client, one file per route group, schemas/ (Zod)
src/lib/<feature>/  pure logic per page, no React
tests/              unit/ (mirrors src/lib), components/ (RTL), contract/ and csp/ (guards), export/
public/theme.js     the theme script, copied to the root of out/ (see Theme below)
e2e/                the browser tests: server.ts (edge stand-in with the CSP and a mock API), fixtures.ts (scenarios), pages.ts, page-watch.ts, csp.spec.ts, fonts.spec.ts, motion.spec.ts, theme.spec.ts, shell.spec.ts, screenshots.ts
e2e/primitives/     a page of every interactive primitive, built and prerendered by Playwright's global setup (build.ts), never part of out/
```

Tests never live in `src/`. `@/*` maps to `src/*`.

## Pages

| Route                                                                                            | Page                                                                                                                                 |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `/`                                                                                              | Apps: the launcher                                                                                                                   |
| `/app/?slug=`, `/app/authorization/`, `/app/deployments/`, `/app/resources/`, `/app/requests/`   | one app: Overview (the earlier Status), Authorization (the earlier Sharing), Deployments (the earlier Releases), Resources, Requests |
| `/app/logs/`, `/app/security/`, `/app/environment/`, `/app/insights/`, `/app/settings/`          | one app: Logs, Security, Environment, Insights, Settings; Security and Settings are built, the rest say they are being built         |
| `/platform/`, `/platform/apps/`, `/platform/access/`, `/platform/egress/`, `/platform/settings/` | the platform: Overview (inbox, memory, self-check), Apps, Access, Egress, Settings                                                   |
| `/dashboard/`                                                                                    | metrics                                                                                                                              |

The ten sections of an app sit behind a sidebar (`AppSidebar`), the five pages of the platform
behind the same kind of sidebar, beside one `SectionPanel`. Each area has a server `layout.tsx` around a client shell
(`AppView`, `PlatformPage`) that fetches and polls once and hands the data to the sub-page
through context, so moving between pages keeps the poll and any half-typed form. The sub-pages
are thin (one file per section under `src/components/app/pages/`, and `platform-pages.tsx`); the
section names, icons and counts are in `src/lib/app/sections.ts` and `src/lib/platform/sections.ts`.
A new sub-page is a route, an entry in `e2e/pages.ts` and a sidebar or tab item; the e2e run and
the screenshots then cover it. The earlier `/app/releases/` and `/app/sharing/` are gone: a static
export has no redirects, so an old link there gets the 404 page.

Authorization, Resources and Requests are rebuilt (T202). Authorization has Permissions (the
owner and the grants in one list; who to share with is typed into `PrincipalCombobox`, which
suggests `everyone`, groups, users and service principals from `GET /principals` and always takes
free text) and Identity (as the visitor, the shared service principal or the app's own, which is in
effect, and the delegation request). Resources is a table with a Detach that asks first, and an
inline Add panel in the console's order: the type from `GET /resources/types`, the object from
`GET /resources/suggestions` (read with the visitor's token; a text field when the type has no
list or Databricks gives none), then the permission, the access mode and the key. A request's
Withdraw asks first. The e2e mock stands in for the directory and the object lists; neither has
listed anything real yet.

The launcher and the app's Overview, Deployments, Logs and Environment are built (T201); where
the table above says Logs or Environment is being built, it is out of date for those two. What
each one decides is a pure function, tested without rendering:

- **Launcher** (`components/launcher/app-list.tsx`, `lib/launcher/apps.ts`): crashed, starting
  and stopped apps come first, then the running ones, each by name; each row shows the person's
  level. It has skeleton rows while it loads, a search field from 11 apps, and an empty state
  with the next step ("Request an app" opens the form; a platform admin gets "Register an app",
  a link to Platform > Apps). `GET /apps` has no active release, so the launcher shows none.
- **Overview** (`AppOverview` in `components/app/app-header.tsx`, `lib/app/status.ts`): Start,
  Stop, Restart and Update in one row, with the reason each unavailable one cannot be used; a
  "Needs attention" list whose notices each carry their action (read the logs, see the failed
  release, restart, attach resources); then the facts. A failed target's `failure_hint` is read
  from `GET /apps/{app}/releases/{id}` for that release only.
- **Deployments** (`release-section.tsx`, `release-actions.tsx`): one row menu per release
  (Activate, Rename, Tag, Lock or Unlock, Delete after an inline confirm, View source); an item
  that cannot be used stays in the menu with its reason. A failed release shows its reason, its
  `failure_hint` with a link to Settings, and its output in a block with a copy button.
- **Logs** (`components/app/logs/`, `lib/app/logs.ts`): `GET /apps/{app}/logs` every 2 s while
  the tab is visible and the view is not paused (no calls at all while it is hidden), asking
  after `next_after`; a new `instance` clears the view, at most 2000 lines are kept, and lines
  are text nodes only.
- **Environment** (`components/app/environment/`, `lib/app/environment.ts`): the runtime, then
  Variables and Packages tabs with a search each, or the same data as JSON; a secret shows `***`
  and its value is never in the page.

Their e2e worlds (a person with no app, a Node.js release that failed in a 64 MB budget, a log
that is empty and then gets lines, a log that never started, an environment with secrets) are
in `e2e/fixtures-f1b.ts`.

## Shell

`src/app/layout.tsx` renders the top bar (`TopBar`) and the main navigation (`MainNav`) around
every page. The top bar is the wordmark, then on the pages of one app the breadcrumb
`/ <app> ▾`, then the theme switch. The `▾` is `AppSwitcher`: a `Popover` with a `Command` list
of the apps, fetched when it opens, filtered by name or slug, that goes to the same section of the
chosen app (`appSectionAt` and `appPagePath` in `src/lib/app/params.ts`) or to its Overview. The
breadcrumb shows the route strip until something has given it the display name
(`useAppDisplayName`): `AppView` hands it over as soon as the app answers, and the switcher's list
does too.

Below 768 px `AppSidebar` hides its column and shows one button, "Section: <label>", that opens
the same items in a non-modal popover. An item's optional `countDescription` is the count in
words, read by a screen reader and shown on hover.

### Theme

Light, Dark or System (the default), chosen with `ThemeToggle` and kept in `localStorage`
under `appmux.theme`. `public/theme.js` is a classic script of this origin that the layout loads
blocking in `<head>`, so `data-theme` is on `<html>` before the first paint and no hash is
needed. CSS resolves System: `globals.css` gives the dark tokens to `[data-theme="dark"]` and,
inside `@media (prefers-color-scheme: dark)`, to every `<html>` that is not
`[data-theme="light"]`, so System follows the OS while the page is open and a blocked script still
leaves the OS scheme. The two dark blocks hold the same values; `tests/contract/contrast.test.ts`
fails when they differ. The script is the only copy of the rule and exposes it as
`window.appmuxTheme`; `useThemeChoice` reads it through `useSyncExternalStore`, and the switch is
disabled, and says so, when the script did not load. See
[ADR 0019](../docs/decisions/0019-give-the-admin-ui-its-own-identity-and-a-theme-switch.md).
The dashboard (`/dashboard/`, the platform) and an app's Insights (`/app/insights/`) are built,
and are one `MetricsPanel` (`src/components/dashboard/`, logic in `src/lib/dashboard/`): a range
from 15 minutes to 7 days (a `ToggleGroup`) asked at the finest step that keeps the window within
1440 steps (1 minute up to a day, 5 minutes for 3 days, 15 minutes for 7); the figures strip, each
figure with a sparkline and the change against the previous window (shown only when that window
holds rows for at least 90% of its minutes, `PREVIOUS_WINDOW_MIN_COVERAGE`); the same requests and
response-time charts on both. The dashboard adds the breakdown by app (`GET /metrics/breakdown`,
the 8 busiest apps plus "Other apps"). Each chart frame has its own loading, empty and failed
state; charts read the colour scheme through `useColorScheme`. The platform view asks
`GET /metrics?app=_platform`, not `GET /metrics` without an app, whose answer mixes every app's
rows. The e2e worlds `breakdown-live` and `metrics-week` (a busy hour and a week of eleven apps)
are in `e2e/fixtures-f4.ts`.

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
  the primitives in `src/components/ui/`. Dark mode is the theme switch's (see
  [Theme](#theme)) through the tokens. No CSS modules, no `style` attribute or prop, no `data:` URI. Colours come
  only from tokens: no opacity modifier on a colour, no arbitrary colour value, no
  default-palette colour (`tests/contract/color-utilities.test.ts`); every token pair is
  contrast-checked (`tests/contract/contrast.test.ts`).
- **Radius, type and motion.** Radius is a scale: `rounded-strip` (3 px, the route strip),
  `rounded-control` (8 px, buttons and fields), `rounded-surface` (12 px, panels and
  menus). Text sizes are 13, 14, 16, 20, 24 and 28 px (`text-xs` to `text-3xl`; there is no
  `text-lg`); weights 400, 500 and 600. Motion is only the `animate-*` utilities of
  `globals.css` (`spinner`, `skeleton`, `lamp-breathe`, `menu-in`, `menu-out`), each with one
  animation for `prefers-reduced-motion: no-preference` and a calmer one for `reduce`;
  `tests/contract/motion.test.ts` fails on a rule that would stop one.
- **Bars.** Every item of a navigation (top bar, tab bar, sidebar) carries `bar-item`: a
  2 px bar under it (beside it inside `bar-axis-vertical`), transparent at rest, `input` on
  hover, `primary` on the item with `aria-current="page"`, and no text underline. `bar-item`
  also holds the padding and text size (the padding on the bar's side is the gap between the
  label and the bar), so an item sets no padding of its own; `tests/components/bar-geometry.test.tsx`
  renders the top bar, the Environment tab bar and the sidebar and fails on one that does.
- **Apps and states.** An app's route is a `RouteStrip`; a state is a `Lamp` (a dot and its
  word, through `StatusBadge`), never a coloured pill.
- **Form controls.** `Input`, `Textarea`, `Select`, `Combobox`, `NativeCheckbox`,
  `NativeRadio` inside `Field`. `Select` is the choice from a fixed list; `Combobox` adds
  search and, with `allowCustom`, a typed value. No page uses a native `<select>`. The Radix
  checkbox, radio, select and dialog primitives are not used (see [Primitives](#primitives)).
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

## Colour, type and the wordmark

The identity is the one of [admin-ui-design.md](../docs/product/admin-ui-design.md): a quiet
paper page, one blue-teal action colour (`--primary`, `--link`, `--ring`), and two marks of its
own, the route strip and the lamp. Red is only danger (`--destructive`), so a destructive control
is no longer one hue away from the primary button; it still stays outlined until its confirm
step, and its label says what it does. The header is the page colour with one hairline under it,
in both colour schemes.

The interface face is IBM Plex Sans (variable) and routes, paths and code are IBM Plex Mono,
both self-hosted from npm by `src/app/fonts.css` (latin and latin-ext only, woff2), so the CSP's
`font-src 'self'` holds.

The header shows the word "appmux" in text, with no logo or icon.

### Trademarks

Databricks is a trademark of Databricks, Inc.; appmux is not made or endorsed by Databricks. The
name appears only to say that appmux runs as a Databricks App.

## The CSP

The page runs under `default-src 'none'; style-src 'self'; script-src 'self'` plus the
SHA-256 of each page's inline scripts (the edge computes them from the export; `theme.js` is a
file of this origin and needs none): no `eval`,
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

Four more specs use the same server. `fonts.spec.ts` checks that both Plex families load from
the export's media folder as woff2 with no CSP violation. `motion.spec.ts` holds the launcher's
app list call open and asks Chromium what the spinner and the skeleton animate, once with
`reducedMotion: "no-preference"` and once with `"reduce"`; the names and durations must match
the Motion table of the design. `theme.spec.ts` picks each theme on each OS scheme (light, dark,
no preference) and checks the attribute, the page colour and the reload; it also changes the OS
scheme under an open page and blocks `theme.js`. `shell.spec.ts` opens, filters, chooses from
and closes (Escape) the app switcher at 1280 and 390 px, and does the same with the phone menu at
390 px, row heights included. `page-watch.ts` collects the CSP violations and errors for both.

Dependency vetting (npm versions, publish dates, OSV result, why the `overrides` exist) is
recorded in [research.md D2](../specs/001-appmux-v1/research.md#d2-layout-and-tooling); for the
two font packages, in the Fonts note of
[admin-ui-design.md](../docs/product/admin-ui-design.md#fonts-adr-note). `cmdk`
1.1.1, added for `Command`, was vetted on 2026-11-03: the exact name shadcn's `command.tsx`
imports, published 2025-03-14, no OSV.dev advisory for any version, no install script; its
Radix dependencies dedupe with `radix-ui`.
