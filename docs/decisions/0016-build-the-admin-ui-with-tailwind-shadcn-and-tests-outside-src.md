# Build the admin UI with Tailwind and shadcn, and keep its tests outside `src/`

**Status:** accepted — the owner asked for this stack on 2026-10-31; extends
[ADR 0012](0012-build-services-in-python-and-the-admin-ui-in-nextjs.md), which
fixed the delivery form (a static export under a strict CSP) and is not changed.
**Date:** 2026-10-31
**Deciders:** Liam Lee

## Context

The first admin UI (2026-10-30) was written to ADR 0012 with hand-written CSS
(`am-*` classes and CSS modules), hand-rolled SVG charts, tests that sit beside
the source in `src/`, `node --test`, and one large file for the API client and
its schemas. On 2026-10-31 the owner asked to rebuild it on the stack they use
in their other projects (shadcn, Tailwind, the shadcn chart), to move the tests
out of `src/`, and to restructure the API layer, and said they run that stack on
many projects, so it needs no deep re-checking of the stack itself.

The one constraint that does not move is the CSP of FR-021: `style-src 'self'`
and `script-src 'self'` plus the hash of each page's inline scripts, with no
`'unsafe-inline'` and no `'unsafe-eval'`. Several parts of the usual stack
break it: a prerendered `style=""` attribute, a runtime `<style>` element, and
any `eval`.

## Decision

- **Styling.** Tailwind v4 (`@tailwindcss/postcss`) with a closed token palette
  in `web/src/app/globals.css`: plain hex values, a light set and a dark set
  chosen by `prefers-color-scheme`, no theme script, fixed `--chart-1..6`
  colours. Colours come only from tokens; no opacity modifier on a colour, no
  arbitrary colour value, no default-palette colour.
- **Components.** shadcn (new-york) primitives on the unified `radix-ui`
  package, copied into `web/src/components/ui/` and patched where the CSP
  needs it, with the page kit (`PageHeader`, `Field`, `DataTable`, …) in
  `web/src/components/` on top. Where a Radix primitive emits an inline style
  or a runtime `<style>` (checkbox, radio, select, tabs, scroll area, modal
  scroll lock), the native control is used instead (`NativeSelect`,
  `NativeCheckbox`, `NativeRadio`).
- **Charts.** The shadcn chart component over Recharts, patched to emit no
  runtime `<style>`; series use the fixed chart tokens, and every chart keeps a
  data table and non-colour cues.
- **Tests.** All tests live in `web/tests/`: `unit/` (mirrors `src/lib`),
  `components/` (React Testing Library in jsdom), `contract/` and `csp/` (the
  guards below), `export/` (guards over the built `out/`); vitest runs them as
  three projects. Playwright runs one real-browser smoke test of the built
  export against the reference CSP.
- **Guards.** Four layers hold the CSP: an ESLint ban on the `style` prop and
  `dangerouslySetInnerHTML`; the syntax-tree scan of `src/`; the rendered-markup
  test over every primitive and kit component; the built-export tests. A colour
  contrast test covers every token pair in both schemes.
- **API layer.** `web/src/lib/api/` holds a client, one file per route group and
  `schemas/` (Zod); the schema graph must stay resolvable by plain Node
  (relative `.ts` imports, `z` only from `schemas/zod.ts`, which sets
  `jitless`), because a test builds it in a child process and fails if anything
  constructs a `Function`.
- **Package manager.** npm stays; the committed `package-lock.json` is the
  cooldown control (every install starts from it).
- **Folder.** `ui/` is renamed `web/`.

## Alternatives considered

- **Keep the hand-written CSS** — not chosen: the owner's stack is shadcn and
  Tailwind, and the dead `am-*` classes would have to be kept in step by hand.
- **Loosen the CSP to allow inline styles** — rejected: FR-021 and the
  constitution; a child that reaches the admin origin must not be able to rely
  on an `unsafe-inline` allowance.
- **ECharts for charts** — not chosen: appmux needs a few line and column
  charts and the owner asked for the shadcn chart.
- **`sonner` or the default `react-toastify` entry for toasts** — rejected: both
  insert a `<style>` element at import. Toasts are not used; `Notice` and the
  live region cover the need.
- **`clsx` plus `tailwind-merge` instead of the `cn` package** — the dependency
  vetting advised this because `cn` 0.4.0 is a young 0.x package whose name was
  reused after sitting idle since 2013; the owner uses `cn` in their other
  projects and the package passed the checks (provenance, no install script, no
  dynamic code in `dist/`), so it stays. Swapping is a one-line change in
  `web/src/components/ui/`.
- **Switch to pnpm for its `minimumReleaseAge`** — not now: npm 11.6.2 has no
  equivalent setting, but a committed lock plus a vetting step per added package
  does the same job; revisit with CI.

## Consequences

**Better:**
- The same components, tokens and test layout as the owner's other projects.
- Tokens are contrast-checked in both schemes, which shadcn's defaults are not
  (`--input` 1.26:1, `--ring` 2.58:1).
- Tests outside `src/` keep the shipped tree small and let the guards scan `src/`
  without skipping test fixtures.

**Worse:**
- Several shadcn primitives cannot be used as shipped, so the UI diverges from
  the owner's other projects in those places, and a future `shadcn add` for the
  chart or a primitive must be re-patched.
- Recharts adds about 150 lock entries and bundle weight to the supply-chain
  surface.
- `cn` is a young 0.x dependency that the vetting would not have chosen.
- The token set is maintained by hand.

**Must now be true:**
- No `style` attribute or prop, no runtime `<style>`, no `eval` in `web/src/`;
  the four guards stay green and are never weakened to pass.
- A new primitive in `web/src/components/ui/` comes with a sample in
  `web/tests/csp/rendered-markup.test.tsx`; a new dependency is vetted as in
  [research D2](../../specs/001-appmux-v1/research.md#d2-layout-and-tooling).
- The built export loads in a real browser under the reference CSP with no
  violation (T176).

## Verification (2026-10-31)

- `npm run test:e2e` passes 69 tests on the fresh export: the launcher, app, platform and dashboard pages and the 404 page, at 1280 and 390 px, light and dark, with no CSP violation, no console or uncaught error, no horizontal overflow and no squeezed table. The four pages read their data from the mock API and show it, so the Zod schemas run under the policy. Four negative controls (an inline style, an inline script outside the hash list, a missing hash, an overflow) fail as intended, and so did two mutations of the harness (dropping one script hash; adding a `style` attribute to the export).
- Recharts raised no violation on the dashboard. A read of the source of Recharts and its `es-toolkit` imports found no `eval` or `new Function` on the paths the dashboard uses; that is a static read, not a check of the bundle.
- The screenshots showed the app page's tables squeezed to a few characters per column at 390 px; `DataTable` now sets a minimum width by column count, so a narrow screen scrolls the table, and the e2e run checks it.
- Cost: each page loads 295 to 307 KB of JavaScript gzipped (1.0 to 1.1 MB raw); the dashboard loads 399 KB gzipped, about 90 to 105 KB more than the other pages, which is Recharts.
- Not covered: only the first load of each page (no poll cycle, click, form or navigation), only Chromium, no real edge, no container ([TD-011](../tech-debt.md)).

## Verification (2026-11-05)

Closes the first "Not covered" items above (T176, T177):

- `npm run test:e2e` now passes 810 tests on a fresh export: each
  page at 1280 and 390 px, light and dark, with no CSP violation, console error, overflow or squeeze.
- The same export, served by the real edge in the Free Edition dev App and opened in Chromium through the
  Apps proxy, loaded under the real `csp.py` policy (no `unsafe-inline`, no `unsafe-eval`) with no
  `securitypolicyviolation`, no cross-origin request, working polling and the isolation headers
  (`tests/integration/test_ui_on_container.py`, 7 passed). That is the "built export loads in a real browser
  under the reference CSP" item above, now on the real edge and not only a stand-in.
- Still not covered: a browser session's own token reaching the folder browser and the resource pickers
  (the checks used the CLI token's owner).

## Revisit if

The browser smoke test finds a CSP violation in Recharts or a Radix-based
primitive that cannot be designed around; the `cn` package changes owner or
ships unexpected code; or CI adopts pnpm.
