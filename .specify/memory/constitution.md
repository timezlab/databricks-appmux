# appmux Constitution

appmux is an open-source reverse proxy that runs as a single Databricks App and
lets many child apps share its slot. These principles bind every qualifying
feature spec, plan, and implementation in this repository.

## Core Principles

### I. Child Apps Stay Standard Databricks Apps

A child app keeps its own `app.yaml` and remains deployable on its own as an
ordinary Databricks App. appmux never requires a child's `app.yaml` or source
to change; everything appmux needs to know about a child (source path,
configuration) lives in the registry entry. Joining appmux is a registry
entry, nothing more.

### II. Health-Gated Routing

A child's route opens only after appmux has fetched its source, built its
environment, started it, and seen it pass its health check — in that order.
No code path adds a route for a child that has not passed health, and a
failure at any earlier step leaves the route closed.

### III. Non-Disruptive Change

Adding or updating a child never interrupts apps that are already running.
A change is observable to existing routes only after the new or updated child
is healthy; until then, traffic keeps flowing exactly as before.

### IV. Documentation Ships with Code

Public surfaces, module boundaries, accepted decisions, and known debt update
in the same change as the implementation that alters them. "Clean up docs
later" is not an accepted state; a change that invalidates a document without
fixing it is incomplete.

### V. Observable Verification

A completion claim names the command or observable check that proves the
outcome, and that check has been run. "Should work" is not done; done is a
passing command, a resolving link, or a visible behavior.

## Documentation Governance

- Accepted ADRs under `docs/decisions/` are append-only: a reversed decision
  gets a new ADR with bidirectional supersession links, never an edit to
  history.
- Spec Kit is used only above the size gate (roughly: three or more tasks, more
  than two days, changed user-visible behavior or contracts, or a debatable
  done condition). Below the gate, make the change directly.
- When a qualifying feature ships, its spec graduates: durable decisions become
  ADRs, project-specific gotchas become reference docs, deferred shortcuts move
  to `docs/tech-debt.md`, and living entry points update in the same change.

## Governance

This constitution supersedes ad-hoc practice for all qualifying features.
Amendments are made via `/speckit-constitution` or by commit with a dated
rationale in the commit message and a version bump below. Reviews of specs,
plans, and implementations verify compliance with the five principles before
acceptance.

**Version**: 1.0.0 | **Ratified**: 2026-10-29 | **Last Amended**: 2026-10-29
