# Split the admin UI into sub-pages and use Databricks red

**Status:** accepted — the owner asked for this redesign on 2026-10-31; extends
[ADR 0016](0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md),
which fixed the stack and the CSP guards and is not changed, and
[ADR 0012](0012-build-services-in-python-and-the-admin-ui-in-nextjs.md).
**Date:** 2026-10-31
**Deciders:** Liam Lee

## Context

After the rebuild of ADR 0016 the platform page was about 9,000 px tall at
1280 px: the inbox, memory, self-check, apps, register form, delegations,
egress rules, admins and thirteen settings forms stacked in one scroll, and the
app page stacked seven sections. An admin who opens the platform page to answer
a request had to scroll past everything else to find out whether there was one,
and nothing showed a result far from the button that caused it. The owner asked
for a primary colour of Databricks red, the Databricks logo beside the
"appmux" wordmark, pages split for scannability, and better flows (feedback
after an action, confirmation of destructive actions, navigation, empty,
loading and error states). The CSP of FR-021, the frozen admin API and the
spec's safety properties do not move.

## Decision

- **Sub-pages.** Each of the app and platform pages becomes five routes behind
  a tab bar (`SubNav`, links with `aria-current`, not ARIA tabs):
  - app (`/app/?slug=`): Status, Releases, Resources, Sharing, Requests;
  - platform: Overview (inbox, memory, self-check), Apps (per-app rows,
    register), Access (platform admins, delegations), Egress, Settings (five
    groups with jump links).
  A nested server layout wraps a client shell that fetches and polls once, so
  moving between tabs does not restart a poll or lose a half-typed form; each
  sub-page is its own static route with its own inline scripts (so its own hash
  under the CSP). `?slug=` stays the only way to name an app. The launcher is
  titled "Apps"; the dashboard is unchanged.
- **What each page shows first.** Platform: what waits for an answer (the inbox;
  its tab carries the number of pending requests). App: what needs attention
  (a crash, a failed release, missing requirements; the Status tab carries the
  count) and the next action. A badge always means something to do.
- **Databricks red.** `brand` is `#ff3621`, the fill of the official icon.
  White on it is 3.62:1, so it paints only non-text marks: the logo, the
  selected-tab bar, the focus ring. `primary` (filled buttons, white text) is
  `#cf2914` in light mode (5.28:1) and `#ff3621` with near-black text in dark
  mode (5.42:1); `link` is `#c42410` (5.82:1 on white) and `#ff8a7a` (8.12:1)
  in dark. The header is a dark band (`chrome`, `#1b1f27`, the same in both
  schemes), where `brand` reads at 4.56:1 (5.41:1 on the darker dark-mode
  band).
- **Red is also danger, so danger never relies on hue.** A destructive control
  is outlined at rest (`danger`), becomes filled only in its confirm step
  (`danger-filled`) inside a framed question with a warning mark, and its
  label says what it does ("Yes, deny request #4"). Failed and stopped states
  keep their word and mark.
- **Logo.** The official Databricks icon (the SVG from databricks.com's press
  kit, `https://www.databricks.com/sites/default/files/2024-10/Databricks-Logo-Icon.svg`,
  sha256 `7de87fef1567963690991896469ad7ed29e29c57c12c42822fca78e3f09f40e2`),
  inlined unmodified as an `<svg>` path in `databricks-mark.tsx`, filled with the
  `brand` token, decorative (`aria-hidden`). Inline, because a `data:` image is
  refused by `img-src 'self'` and a file would be one more request. It marks
  where appmux runs; the wordmark beside it reads "appmux for Databricks Apps".
  The README carries the trademark line: Databricks is a trademark of
  Databricks, Inc.; appmux is not made or endorsed by Databricks.
- **Behaviour.** **Deny** in the inbox asks first (a denial is final for the
  asker, as Delete and Detach already ask); a passing self-check folds its
  twelve rows and opens them when a check fails; table and chart scroll frames
  are keyboard-focusable named regions (TD-011); table cells break a long
  value only when it cannot fit on its own line (`wrap-break-word`), so an
  email is no longer split in the middle in a table that has room; dashboard
  charts fill the page column; the inbox card puts the request on the left and
  the answer on the right at wide widths; settings cards sit two to a row.
- **No new dependency, no new font.** The system font stack stays.

## Alternatives considered

- **Keep one page per role and add anchors** — rejected: it keeps the 9,000 px
  scroll and every poll re-renders every form.
- **ARIA tabs (a Radix tabs primitive)** — rejected: Radix tabs emit inline
  styles the CSP refuses (ADR 0016), and routes are more useful: each tab is
  linkable and has a title.
- **One client route per page with the tab in the query string** — rejected:
  one route would carry every tab's scripts and one hash, and `?slug=` is
  already the one query parameter.
- **`#ff3621` for filled buttons and link text** — rejected: 3.62:1 with white
  text fails the 4.5:1 rule; the darker shades keep the hue and pass.
- **A webfont for the wordmark** — not done: it needs a vetted self-hosted file
  and a licence check; an owner decision, none proposed here.

## Consequences

**Better:**
- The platform overview is about 1,900 px tall at 1280 px (was 9,054 px for the
  whole page), and the inbox is the first thing on it.
- Deny, the one destructive action that answered at once, now asks first like the rest.
- Keyboard users can scroll a wide table or chart.

**Worse:**
- More routes: 12 pages in the export (were 4), each with its own inline-script
  hash, so the e2e matrix and the screenshot run are larger.
- Red is both brand and danger; the separation rests on outline vs filled, the
  confirm step and wording, which a future component must keep.

**Must now be true:**
- Every sub-page stays in `web/e2e/pages.ts`; a new component has a sample in
  `web/tests/csp/rendered-markup.test.tsx`; every new colour token has contrast
  pairs in `web/tests/contract/contrast.test.ts`.
- The admin API and its Zod schemas are unchanged by this decision.

## Revisit if

The owner picks a webfont; the app page needs a sixth tab; or user testing shows
the red primary and the red danger confusable.
