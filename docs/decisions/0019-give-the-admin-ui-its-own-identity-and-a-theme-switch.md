# Give the admin UI its own identity and a theme switch

**Status:** accepted 2026-11-06 on the owner's word to follow the builder's recommendation (it was
proposed from 2026-11-03). The recommendation was to keep what is built: the wordmark stays plain
text with no glyph, and the default theme stays System. It was seen on the dev App in the T205
hand run of 2026-11-05 (by the builder, as a platform admin, through the real proxy; see
[Seen on the dev App](#seen-on-the-dev-app-2026-11-05)). Its motion rule (a behaviour
for both values of `prefers-reduced-motion`) is superseded by [ADR 0021](0021-ignore-prefers-reduced-motion-in-the-admin-ui.md).
Supersedes three parts of [ADR 0017](0017-split-the-admin-ui-into-sub-pages-and-use-databricks-red.md)
(the Databricks red, the dark chrome band and the Databricks logo) and nothing
else of it; extends [ADR 0018](0018-settle-the-admin-ui-layout-rules-and-give-settings-one-save-bar.md)
and [ADR 0016](0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md),
whose stack and CSP guards are not changed.
**Date:** 2026-11-03
**Deciders:** Liam Lee

## Context

The owner hand-tested the admin UI on the Free Edition dev App as an app admin
and as an end user and gave eleven points of feedback
([design](../product/admin-ui-design.md)). Two of them shape this record: the
UI should work like the Databricks Apps console (a left sidebar, one area per
concern), and it should have a theme switch (point 7). The red of ADR 0017 also
made the primary button one hue away from every destructive control, so the
danger cues rested on outline versus fill alone. The CSP of FR-021 does not
move: `script-src 'self'` plus hashes, `style-src 'self'`, no inline style
attribute in prerendered HTML.

## Decision

- **Identity: a patch bay.** A telephone exchange routes many lines through one
  panel; appmux routes many apps through one App. Two marks carry that and
  everything else stays quiet: the **route strip** (each app's `/slug/` on a
  paper-coloured strip in the monospace face, wherever an app is named) and the
  **lamp** (a round lamp plus a word for the state, never colour alone). The
  primary is **cord teal** (`#0b6a84` light, `#4cc3e2` dark); danger is the
  only red. The faces are **IBM Plex Sans and Plex Mono**. The tokens and their
  contrast pairs are in the design doc; `tests/contract/contrast.test.ts` is
  the judge.
- **Information architecture: the Databricks Apps console, appmux's skin.**
  One app and Platform each get a left sidebar of areas (Overview,
  Authorization, Deployments, Logs, Security, Environment, Resources, Insights,
  Requests, Settings for an app), the top bar carries `appmux / <app> ▾` with a
  searchable app switcher, and the content is one inset panel. Colour, type,
  the strip and the lamp are appmux's. ADR 0017's sub-page split stays: every
  area is its own static route behind `?slug=`; its tab bar becomes the sidebar.
- **What of ADR 0017 is superseded.** Only the Databricks red (`brand`
  `#ff3621` and the red `primary` and `link`), the dark chrome band of the
  header (`chrome-*`) and the Databricks logo (`databricks-mark.tsx`). The
  header is now the page colour with one hairline under it, and the wordmark
  is the plain text "appmux".
- **Fonts.** `@fontsource-variable/ibm-plex-sans` 5.3.0 and
  `@fontsource/ibm-plex-mono` 5.3.0, OFL 1.1, pinned exactly and self-hosted
  under `font-src 'self'`. Vetted before install: published 2026-07-19, no
  install scripts, no dependencies, no OSV advisory (none of the `MAL-` kind),
  published from the `fontsource/font-files` repository. Six woff2 faces ship
  (Sans variable latin and latin-ext, Mono 400 and 500 in both), measured at
  133,052 bytes in the export; an English page loads about 75 KB. ADR 0017 left
  a webfont as an owner decision in "Revisit if"; this is that decision.
- **No animation library.** `tw-animate-css` is removed. Every enter and exit
  animation comes from the motion table of the design doc (`animate-menu-in`,
  `animate-menu-out` and the rest), each defined once in `globals.css` with a
  behaviour for both values of `prefers-reduced-motion`.
- **The theme switch: Light, Dark or System, System by default.**
  - `web/public/theme.js`, a same-origin classic script loaded blocking from
    `<head>`, reads the stored choice (`localStorage` `appmux.theme`, read and
    written in try/catch) and writes it as `data-theme` on `<html>` before the
    first paint. It needs no hash: `script-src 'self'` allows a file of this
    origin, and the edge already serves `.js` from the export.
  - CSS resolves System, not script: `data-theme="dark"` and, inside
    `@media (prefers-color-scheme: dark)`, any `<html>` that is not
    `data-theme="light"` get the dark tokens. So System follows the OS live
    while the page is open, and a visitor whose script is blocked still gets
    the OS scheme. The dark values are written for both selectors; the
    contrast test fails when they differ and judges every pair for each.
  - The script is the one copy of the rule (parse, store, apply, follow
    another tab's change) and exposes it as `window.appmuxTheme`. The React
    switch (`ThemeToggle`) only calls it; `tests/unit/theme-script.test.ts`
    runs the file itself in jsdom.
  - `<html>` carries `suppressHydrationWarning`, because the attribute is set
    before React hydrates by design; it covers that element's own attributes
    only.
- **The wordmark is plain text, and its glyph is the owner's open decision.**
  No mark is drawn until the owner picks one.

## Alternatives considered

- **Keep ADR 0017's red** — rejected: the primary and danger share a hue, the
  cost ADR 0017 itself listed.
- **Resolve System in the script (`matchMedia` plus a listener)** — rejected:
  a second copy of what CSS already does, and it fails dark-OS visitors whose
  script is blocked.
- **CSS `light-dark()` for every token** — rejected: every token line would
  carry both values, which the contrast test and the design doc's table read as
  one value each; two blocks kept the change mechanical.
- **`next/script` with `beforeInteractive`, or an inline script with its hash**
  — rejected: in the App Router the first is pushed through Next's own inline
  bootstrap (`self.__next_s`), not written as a blocking tag, so it needs a
  hash and runs late; the second adds a hash to every page and a second place
  the rule lives.
- **A cookie read by the server** — rejected: the admin UI is a static export
  with no server rendering.

## Consequences

**Better:**
- The primary no longer looks like a destructive control; danger is the only red.
- A person can choose a theme; the page never flashes the other one, and System
  tracks the OS without a reload.
- appmux no longer carries another company's logo or colour.

**Worse:**
- About 75 KB of fonts on an English page (133,052 bytes in the export).
- One more request per page (`theme.js`, 2.7 KB, mostly its comment), blocking
  in `<head>`, from the same origin.
- The dark token values are written twice in `globals.css`; the contrast test
  is what keeps them equal.

**Must now be true:**
- Every new colour token has its dark value in both dark blocks and contrast
  pairs in `web/tests/contract/contrast.test.ts`.
- The layout's `<script src>` of the theme file is the only script element in
  the source (`tests/contract/markup-rules.test.ts`), and every exported page
  loads it once, blocking, in `<head>` (`tests/export/theme-script.test.ts`).
- `e2e/theme.spec.ts` keeps proving each choice on each OS scheme, the reload,
  the attribute at DOMContentLoaded and the blocked-script case.

## Seen on the dev App (2026-11-05)

The admin UI on the Free Edition dev App was driven in Chromium through the real Apps proxy, signed in
as the CLI token's owner (a platform admin): the theme switch in dark, light and system, the choice
surviving a reload, and both themes at 390 px, with no CSP violation, page error or console error
([spike log, T205](../../specs/001-appmux-v1/spikes/README.md#the-admin-ui-by-hand-through-the-real-proxy-t205)).
The status stays `proposed`: the owner has not reviewed the result, and the two choices named
under "Revisit if" (the wordmark glyph and the default theme) are still the owner's.

## Revisit if

The owner picks a wordmark glyph; the owner wants Light or Dark as the default,
or System to stop following the OS while a page is open; or a later CSS feature
makes the duplicated dark block avoidable without a script.
