# Settle the admin UI's layout rules and give Settings one save bar

**Status:** accepted 2026-11-06 on the owner's word to follow the builder's recommendation
(it was proposed from 2026-10-31). It carries out the owner's feedback on the second design
round of 2026-10-31, and the owner hand-tested the result on the dev App on 2026-11-03. The
choices the builder made inside it stay as built: the 8-app search threshold and the direct
Remove button in the egress list. The two red primaries on the app page no longer exist, because
[ADR 0019](0019-give-the-admin-ui-its-own-identity-and-a-theme-switch.md) replaced the red. This
ADR extends [ADR 0017](0017-split-the-admin-ui-into-sub-pages-and-use-databricks-red.md),
whose text is not changed except for one line this ADR replaces (settings cards
sit two to a row, see Decision), and [ADR 0016](0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md),
which fixed the stack and the CSP guards and is not touched.
**Date:** 2026-10-31
**Deciders:** Liam Lee

## Context

After ADR 0017 the pages were split and red, but the owner found the screens
uneven: fields in a form did not line up (a hint above one control, below
another; controls of different heights); Platform > Settings was thirteen cards
with a save button each; the launcher, the page every user opens, explained two
ways to open an app in a long box above a card per app, which does not scale to
a slot that hosts about 100 apps; and the memory figure was a native `<meter>`,
which draws differently in each browser. The CSP of FR-021 and the frozen admin
API do not move.

## Decision

- **One field anatomy.** A label, the control, then the hint or error *below*
  the control (`Field`). Fields sit in one stack (`FORM_STACK_CLASSES`) or in a
  grid whose rows share the controls' baseline (`FieldGrid`); `FormActions` ends
  a form. Every page's forms were audited and converted.
- **One control height and one button order.** Controls and normal buttons are
  h-10; `small` (h-8) only in table rows, dense lists and notices. In any row of
  buttons the primary is last, also inside `ConfirmButton` (Cancel, then the
  answer that goes ahead).
- **One spacing scale.** Gaps between things are 1, 2, 4 or 6 steps (4, 8, 16,
  24 px). Table cells, tabs and buttons keep their own padding.
- **One table pattern.** `DataTable` stacks on a phone by default and scrolls
  (`layout="scroll"`) only for numeric tables. The row's name is the first column,
  *Actions* is the last, left-aligned, and its label is hidden when stacked;
  cells align to the middle so a button and its row's text share a line.
- **The launcher is a table.** Name (with the slug under it), State, Actions;
  sorted by name; a search box from 8 apps; *Open* where it works and the reason
  in plain words where it does not; *Manage* only for managers; the admin
  notice only for platform admins and only when something waits; "Your requests"
  only when there is one; **Request a new app** as a quiet header button; the
  two ways to open explained in a small closed "What is the difference?"
  disclosure, shown only to a person who is offered both. Details are in
  [design 10.1](../product/v1-design.md#101-screens).
- **Settings is a dense list with one save bar.** This replaces ADR 0017's "settings cards
  sit two to a row". One row per setting; one sticky bar at the bottom saves every
  changed row with the one write the API has (`PUT /platform/settings/{key}`), one
  call per row, reporting "2 of 3 saved, 1 failed" and leaving a failed row marked
  and editable. No API change.
- **Memory is a flat square strip**, not `<meter>`: 20 cells counted from the
  figures, with a percentage and a word beside it, so it needs no inline width
  and no colour carries the fill alone.

## Alternatives considered

- **Keep a Save button per setting** — rejected: thirteen buttons for one
  mental task, and no way to see what is unsaved.
- **A batch settings endpoint** — rejected for now: it changes the frozen API for
  a UI gain, and the per-row result is useful on its own. Revisit with the
  settings version below.
- **A modal or a card grid for the launcher** — rejected: neither scans a long,
  sorted list, and a modal needs focus handling the CSP-strict kit avoids.

## Consequences

**Better:**
- A form looks the same on every page; a long app list can be scanned and
  searched; the settings page says what is unsaved and what failed.

**Worse:**
- Saving many settings is several calls, not one transaction; a partial save is
  possible and shown. The contract has no version on a setting, so two admins
  saving the same key is last-write-wins, with a warning on the row.

**Must now be true:**
- A new form uses `Field`, `FieldGrid` or `FORM_STACK_CLASSES`, and `FormActions`
  with the primary last; a new table uses `DataTable` with an *Actions* column
  last. The rules are in `web/README.md` and the e2e run fails on a table that
  is squeezed, clipped or has a caption narrower than itself.
- The launcher's reasons and thresholds stay in `src/lib/launcher/`, tested
  without rendering.

## Revisit if

The contract gains a version on settings (then a stale row can be refused, not
just warned about); a slot hosts far more than about 100 apps (then the list
needs paging); or the owner wants a single settings transaction.
