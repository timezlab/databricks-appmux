# Adopt spec-driven development with GitHub Spec Kit

**Status:** accepted
**Date:** 2026-10-29
**Deciders:** Liam Lee

## Context

appmux is an open-source project that will be built largely with coding agents.
Its core is a multi-step child-app lifecycle (fetch, build, start, health
check, open route) with several unsettled design questions
([DESIGN.md](../product/DESIGN.md#decisions-and-open-questions)). Work of that shape spans
many sessions and contributors, and a plan that lives only in a chat is gone
at the next session.

## Decision

Use GitHub Spec Kit (`specify` CLI, Claude integration) for features that clear
the size gate — three or more tasks, more than two days, changed contracts, or
a debatable done condition. Each such feature gets `specs/NNN-<slug>/`
(`spec.md`, `plan.md`, `tasks.md`) driven by the `/speckit-*` skills. Standing
principles live in `.specify/memory/constitution.md`. Work below the gate gets
no spec. When a feature ships, its durable knowledge graduates into `docs/`
(decisions to ADRs, gotchas to reference, shortcuts to the debt log).

## Alternatives considered

- **Plan mode only, no persisted specs** — kept for work below the gate;
  rejected as the only mechanism because the plan does not outlive the session.
- **Hand-written spec templates** — rejected: Spec Kit already provides
  templates, scripts, and the command workflow, and a home-grown copy would
  have to be maintained.

## Consequences

**Better:**

- A feature's intent, plan, and task list survive across sessions and
  contributors, in the repository.
- The constitution gives every feature the same standing principles to check
  against.

**Worse:**

- Ceremony cost: a spec written for work below the gate is context noise for
  every later session, so the gate must be applied honestly.
- Spec Kit ships weekly; commands and paths can change under us.

**Must now be true:**

- Features that clear the gate start with `/speckit-specify` and live in
  `specs/NNN-<slug>/` at the repository root; specs are ephemeral history once
  shipped.
- `.specify/scripts/` and `.specify/templates/` are Spec Kit–managed; upgrade
  them with `specify self upgrade`, not by hand-editing.
- Principles that must hold go in `.specify/memory/constitution.md`;
  `AGENTS.md` only routes to it.

## Revisit if

Specs repeatedly drift from the code despite `/speckit-converge`, or the gate
keeps being judged too heavy for the features this project actually has.
