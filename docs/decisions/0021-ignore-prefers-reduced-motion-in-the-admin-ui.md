# Ignore prefers-reduced-motion in the admin UI

**Status:** accepted — the owner asked for it on 2026-11-04 ("bỏ mọi prefers-reduced-motion để nó work với mọi
value"). Supersedes the motion rule of [ADR 0019](0019-give-the-admin-ui-its-own-identity-and-a-theme-switch.md)
("a behaviour for both values of `prefers-reduced-motion`") and nothing else of it.
**Date:** 2026-11-04
**Deciders:** Liam Lee

## Context

ADR 0019 gave every animation two behaviours: the normal one for `no-preference` and a calmer one for
`reduce`. For the spinner the calmer one was a pulse of its opacity instead of a turn. The owner's system
asks for reduced motion, and on the dev App the spinner of a button that was saving did not turn; the owner
read that as a spinner that does not work, twice (2026-11-03 and 2026-11-04). The first fix removed the
blanket `0.01 ms` rule that had stopped it; the second shows that a different animation is not wanted either.

## Decision

- **The admin UI does not read `prefers-reduced-motion`.** `globals.css` has no `@media (prefers-reduced-motion)`
  block, no component uses a `motion-reduce:` or `motion-safe:` utility, and no script reads the preference.
  Every animation of the motion table plays the same for every value.
- **The motion stays small on purpose.** The spinner turns (0.9 s), the starting lamp and the skeleton pulse
  their opacity, a menu fades in with a 4 px slide, and a bar changes colour. Nothing slides across the page,
  nothing parallaxes, nothing plays on its own except the spinner, the lamp and the skeleton, each of which says
  that something is happening.
- **A test keeps it that way.** `web/tests/contract/motion.test.ts` fails when a query, a utility or a script
  that reads the preference comes back, and `web/e2e/motion.spec.ts` runs Chromium under both values of
  `reducedMotion` and expects the same animation name and duration.

## Consequences

- A visitor whose system asks for less motion sees the same motion as everyone else. The motion is the small
  set above, not page-level movement, but it is not zero. This is a trade the owner chose for a spinner that is
  always readable as "working"; it is not a claim that the preference does not matter.
- Nothing else in the UI follows an operating-system preference except the colour scheme (ADR 0019).

## Revisit if

A person reports discomfort from the motion. The change to make then is a "Reduce motion" switch the person
sets in the UI, next to the theme switch (stored the same way, applied as an attribute on `<html>`), not a
return of the operating-system signal.
