# appmux

A reverse proxy that runs as a single Databricks App and lets many child apps
share its slot.

> **Status: pre-alpha.** Implementation has started: settings, identity, the
> registry models and migrations, the egress rules, the deploy scripts and the
> admin UI scaffold exist, and nothing has run in a Databricks container yet
> ([ARCHITECTURE.md](ARCHITECTURE.md) marks what exists). Everything under *How it
> works* describes the intended behavior, not shipped behavior.

## Why appmux

A Databricks workspace caps how many apps it can host (100), and every app
holds a slot and always-on compute. appmux puts many apps behind one slot:

- **No 100-app ceiling per workspace** — child apps share appmux's slot instead
  of each taking their own.
- **Less always-on compute** — one running app instead of one per child.

## How it works

Each child app keeps its own `app.yaml` and can still be deployed on its own
as a normal Databricks App. To bring one into appmux, register it in the
registry with its source path and configuration. appmux then:

1. downloads the child's source,
2. builds its environment,
3. starts it,
4. checks its health,
5. and only then opens its route.

Because the route opens last, adding or updating a child does not interrupt the
apps that are already running.

## Working in this repository

- Coding agents start at [AGENTS.md](AGENTS.md); the agent harness (skills,
  agents, rules, hooks) is described in
  [docs/reference/agent-harness.md](docs/reference/agent-harness.md).
- Features that clear the size gate go through GitHub Spec Kit — see the
  *Spec gate* in [AGENTS.md](AGENTS.md).

## Documentation

- [docs/index.md](docs/index.md) — map of all documentation
- [docs/product/DESIGN.md](docs/product/DESIGN.md) — product direction, goals,
  non-goals, and the ADR behind each early question
- [docs/product/v1-design.md](docs/product/v1-design.md) — what appmux v1 is and
  how it behaves (planned, not implemented)
- [specs/001-appmux-v1/spec.md](specs/001-appmux-v1/spec.md) — the v1 feature
  spec and the spikes that gate its plan
- [ARCHITECTURE.md](ARCHITECTURE.md) — intended module map and invariants

## License

Apache-2.0 — see [LICENSE](LICENSE) and
[ADR 0013](docs/decisions/0013-license-appmux-under-apache-2-0.md).
