# Admin UI: visual identity and interaction rules

Status: proposed 2026-11-03. It answers the owner's eleven points of feedback from a hand test as an app admin
and an end user on the Free Edition dev App. The decision record is
[ADR 0019](../decisions/0019-give-the-admin-ui-its-own-identity-and-a-theme-switch.md); the backend it needs is
the addendum at the end of [the admin API contract](../../specs/001-appmux-v1/contracts/admin-api.md); the work is
tasks T187 onward of [tasks.md](../../specs/001-appmux-v1/tasks.md). The constraints that do not move are the CSP of
FR-021 ([ADR 0016](../decisions/0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md)) and the
frozen shape of what already exists in the admin API.

## What the UI is for

appmux puts many apps on one line, so the admin's job is to see which of them is alive, change one without touching
the others, and say who may open it. Three people use it: an end user opens an app, an app admin ships and shares
one, a platform admin keeps the slot healthy. Each opens it to do one small job and leaves, so the screens must be
quiet, fast to read, and honest about state, not a showcase.

## Direction: a patch bay

A telephone exchange routes many lines through one panel: a jack for each line, a typed paper strip under each jack,
a lamp that says whether the line is live. appmux does the same for apps, so the interface borrows two things from
that world and keeps everything else plain:

- **The route strip.** Every app's route (`/sales/`) is written on a small paper-coloured strip with square corners,
  in the monospace face. It is the one place the interface is not neutral, and it appears wherever an app is named
  (launcher rows, app header, grants, requests, dashboard), so a person learns to find "their" app by its strip.
- **The lamp.** State is a small round lamp plus a word, never a pill: Running (green), Starting (amber, breathing),
  Stopped (grey ring), Failed (red). A lamp has no background box. Colour is never the only signal.

Everything around those two is deliberately quiet: no dark header band, no card grid, no gradients, no shadows beyond
the one under a floating menu.

## Tokens (starting values; `tests/contract/contrast.test.ts` is the judge)

Hex only, in `:root` and overridden for dark, as before ([globals.css](../../web/src/app/globals.css)). Danger is
the only red: the old brand red is gone, so a destructive control is no longer one hue away from the primary button.

| Role | Light | Dark | Use |
|---|---|---|---|
| paper (`background`) | `#f5f6f6` | `#0e1316` | page |
| ink (`foreground`) | `#151b20` | `#e4eaed` | text |
| cord (`primary`, `link`, `ring`) | `#0b6a84` (ring `#0a87a8`) | `#4cc3e2` | actions, links, focus, the selected tab |
| strip (`strip`, `strip-foreground`, `strip-border`) | `#f1e8c6`, `#3a3010`, `#cdbf8a` | `#2d2812`, `#ede0a8`, `#5a4e22` | the route strip only |
| lamps | existing `tone-*` sets | existing `tone-*` sets | state words, notices |
| surfaces | `card` `#ffffff`, `muted` `#e9ecee`, `accent` `#e1e6e9` | `card` `#151c20`, `popover` `#182025` | menus, inputs, hover |

The `chrome-*` tokens (the dark header band) are removed. Radius is a scale, not one value: the strip 3 px, controls
8 px, floating surfaces 12 px.

## Type

IBM Plex Sans (variable, self-hosted, latin and latin-ext) for the interface, IBM Plex Mono for routes, paths, keys
and code only. Scale 13 / 14 / 16 / 20 / 24 / 28 px, weights 400, 500, 600; headings 600 with -0.01em tracking;
tabular figures in tables and charts. No capitalised labels: table headers and field labels are sentence case, 13 px,
medium weight, muted. Prose lines stop at 70 characters. The files come from npm (`@fontsource-variable/ibm-plex-sans`,
`@fontsource/ibm-plex-mono`, OFL 1.1), are served by the edge from the export under `font-src 'self'`, and are
measured under the reference CSP before they stay.

## Layout: the Databricks Apps console, in appmux's own skin

The owner asked (2026-11-03) for app and platform administration to work like the Databricks Apps console: a left
sidebar, one area per concern, an Environment page that lists variables and packages, and the same flow to add a
resource. So the information architecture is copied from that console; colour, type, the strip and the lamp are
appmux's.

```
 appmux  / Sales dashboard ▾                                   [☼ theme ▾]  alice@example.com ▾
 Apps  Platform  Dashboard      (top bar: page colour, one hairline under it)
 ┌───────────────┬────────────────────────────────────────────────────────────────────────┐
 │ ‹ All apps    │  Environment                                                           │
 │               │  [Structured | JSON]                                                   │
 │ ▢ Overview    │  ┌ Runtime ───────────────────────────────────────────── [copy] ┐     │
 │ ◧ Authoriz…   │  │ Python 3.11.18 · Node.js v22.23.4                              │     │
 │ ⇪ Deployments │  └────────────────────────────────────────────────────────────────┘     │
 │ ▤ Logs        │  Variables (12)  Packages (134)                  [Search…] [copy]     │
 │ ⛨ Security    │  Key                     Value                                         │
 │ ⚭ Environment │  UVICORN_PORT            8000                                          │
 │ ◫ Resources   │  …                                                                     │
 │ ◔ Insights    │                                                                        │
 │ ✉ Requests ②  │   one inset panel on the page colour; sections are ruled, not boxed   │
 │ ⚙ Settings    │                                                                        │
 └───────────────┴────────────────────────────────────────────────────────────────────────┘
```

- **The sidebar** appears inside one app and inside Platform; Apps and Dashboard are full-width pages. It has a
  "‹ All apps" link on top, icon plus label per item, a count only where it means something to do (Requests), and a
  collapse button that leaves icons only (kept in `localStorage`, guarded). Below 768 px it becomes one menu button
  in the page header ("Section: Environment") that opens a non-modal popover list. The top bar carries the breadcrumb
  `/ <app> ▾`, a switcher that opens a searchable list of apps (`Command` in a `Popover`).
- **The content** is one inset panel (`card`, 12 px radius, hairline border) on the page colour, 1120 px at most, left
  aligned; forms stop at 560 px. Inside it sections are ruled, not boxed into further cards.
- **One geometry for every bar** (point 8): the top navigation, the sidebar items and the Variables/Packages tabs share
  one rule, a 2 px bar that is transparent at rest, muted on hover and cord on the selected one, and none of them has a
  text underline.
- **The dashboard** keeps its own page; its six identical figure cards become one strip of figures divided by
  hairlines, each with a sparkline. The same view, scoped to one app, is the app's Insights.

### The app's sections (each is a static sub-route of `/app/?slug=`)

| Section | What it holds | Replaces |
|---|---|---|
| Overview | state lamp, route strip with Open app, active release, runtime, memory budget, source folder, owner, what needs attention (failed release, missing requirements), Start, Stop, Restart, Update | Status |
| Authorization | Permissions: who can open and who can manage (add a user, group or service principal from one combobox with a level select; change a level in place; remove), then Identity: whether the app acts as the visitor (OBO) or as a shared service principal, and the request to ask for it | Sharing and the delegation part of Platform > Access |
| Deployments | the releases as a list with state, number, name, tags, lock, when and by whom; Update; a row menu for Activate (roll back), Rename, Tag, Lock, Delete, View source; the failure reason and the child's last output for a failed one | Releases |
| Logs | the child's output (see below) | new |
| Security | the app's egress rules (read-only for an app admin, editable for a platform admin) and what the sandbox forbids, in words | Platform > Egress per app |
| Environment | Structured and JSON views; a Runtime block (Python and Node versions); Variables (name, value, where it comes from: the release config, a resource, or the platform) and Packages (declared by the release, resolved wheels, and what the platform provides), each with search and copy; a secret value is shown as `***` | new |
| Resources | the attached resources as a list, and **Add resource** as in the Databricks console (see below) | Resources |
| Insights | the dashboard view for this app | Dashboard scope select |
| Requests | the requests for this app, with a count | Requests |
| Settings | display name, slug, source folder (with the folder browser), memory budget, owner, delete app; an app admin who may not change a field sees it read-only with "Request a change" | the platform fields of Status |

Platform keeps its five areas (Overview, Apps, Access, Egress, Settings) as a sidebar of the same kind.

## Components

- **shadcn first.** A component that shadcn ships (Select, Popover, Command, DropdownMenu, Tabs, Tooltip, Collapsible,
  Sonner-style toast where useful) is taken from shadcn's source and adapted, not rewritten, and keeps its
  name and props, so a later shadcn update can be diffed. A native `<select>` is used for nothing a person sees
  (point 9); `NativeSelect` stays only where a phone's own picker is the better control, and says so in a comment.
- **CSP rules for those components.** The strict CSP (no inline style attribute in prerendered HTML, no injected
  `<style>`) shapes the set: Radix `Select` and modal `Dialog` inject a `<style>` through `react-remove-scroll`, so
  Select is built on `Popover` and `Command` with shadcn's markup and classes; `DropdownMenu` runs with `modal={false}`;
  anything portalled renders only on the client (`use-is-client`). Each primitive gets a sample in
  `tests/csp/rendered-markup.test.tsx` and is opened in real Chromium under the reference CSP in `e2e/csp.spec.ts`.
- **Theme switch (point 7).** shadcn's theme button: an icon button with a sun and a moon, a menu of Light, Dark and
  System. The choice is kept in `localStorage` (guarded), applied as `data-theme` on `<html>` by a small same-origin
  script in the head, so there is no flash and no inline script to hash. System follows the OS and stays the default.
- **Skeletons.** Every data-dependent block has a skeleton with the shape of what will arrive (table rows, the
  header, a chart frame); a spinner is for an action in progress, not for a page.
- **Principal field (point 1).** A combobox, like Databricks' share dialog: type a name and get groups, users and
  service principals as rows with an icon, the display name and the identifier; type `everyone` and get the
  "everyone" row; typing a full e-mail is always accepted when the directory cannot be read.
- **Folder browser (point 2).** Beside the source folder field: a popover listing the folder's sub-folders with a
  breadcrumb, starting at My folder and Shared; it marks a folder that looks like an app (it holds `app.yaml`,
  `requirements.txt` or `package.json`) and says what that means for memory (Node needs about 768 MB).
- **Logs (point 4).** The Logs section: the last lines of the child's output, polled, with a stream mark,
  pause, wrap and copy, and, for an app that stopped, the exit code and signal.
- **Resources (point 6), the Databricks flow.** **Add resource** opens an inline panel (no modal: a modal dialog needs
  the scroll lock that the CSP refuses). Step by step, as in the console: choose the type from a menu with an icon and
  a one-line description each; choose the object from a searchable list of what the person can see (warehouses,
  serving endpoints, secret scopes and keys, experiments, jobs), loaded as they type; choose the permission from
  the levels that type allows; choose how the app uses it (as the visitor, as the app's own service principal, as the
  shared one), where an unavailable mode is disabled with the reason and the way to ask for it; then the resource
  key, filled from the type and the object and editable. **Add** attaches it, the list shows it, and a refusal shows
  the server's reason in words. Detach is a row action that asks first.
- **Environment (Variables and Packages).** Variables come from three places and say which: the release's config, the
  attached resources (`valueFrom`, secret values never shown), and what the platform sets for every child (the port,
  host and root path). Packages are what the release declares (`requirements.txt` or `package.json`), the wheels
  resolved for it, and what the platform's own environment provides. The JSON view is the same data as text with
  a copy button.
- **Dashboard (points 10 and 11).** The Platform view has the same charts as an app (requests, response time,
  memory) from the platform row, then the same charts broken down by app (stacked by app, top eight and "other").
  Ranges: 15 minutes, 1 hour, 6 hours, 24 hours, 3 days, 7 days; longer ranges are summed per 5 minutes or per hour
  by the server.

## Motion (point 3)

Every animation has a defined behaviour for both values of `prefers-reduced-motion`, and none of them vanishes:

| Motion | `no-preference` | `reduce` |
|---|---|---|
| Spinner | rotates, 0.9 s linear | stays visible and shows a slow opacity pulse (1.6 s), no rotation |
| Starting lamp | opacity breathe, 1.6 s | the same, 2.4 s |
| Skeleton | opacity shimmer 1.4 s | the same, 2.2 s, no travelling gradient |
| Menu, popover, tab bar | 120 to 160 ms fade and 4 px slide, bar slides between tabs | 120 ms fade only, bar jumps |
| Page change | none | none |

The blanket rule that set every animation to 0.01 ms is removed, because it is what stopped the spinner. A test opens
a page in Chromium with each value of `reducedMotion` and asserts that the spinner and the skeleton have a running
animation whose name and duration match the table. No motion plays on its own except the spinner, the starting lamp
and the skeleton, each of which says "something is happening".

## Copy

Sentence case, active verbs, the same name for an action in the button and in the confirmation ("Attach", then
"Attached"). An error says what failed and what to do next; the server's `code` is mapped to a sentence in one place
per page, and an unmapped code shows the server's message, never "The resource was not attached" alone.

## What is not changed

The routes, the static export, the CSP, the single zod schema per API shape, kebab-case files with named exports,
tests outside `src`, no `style` prop (ESLint), the `Button` that refuses `style` and `className`.
