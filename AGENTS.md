# appmux

appmux: open-source reverse proxy that runs as one Databricks App and lets many
child apps share its slot. Pre-alpha — implementation started on 2026-10-30
(see [ARCHITECTURE.md](ARCHITECTURE.md) for what exists).

## Stack

Python 3.11 services + Next.js admin UI as a static export
([ADR 0012](docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md));
built in waves from tasks.md. Harness: Markdown skills/rules/agents + Python 3 stdlib hooks ·
GitHub Spec Kit (`specify` CLI).

## Key rules

- `.agents/` holds the real skills, agents, rules, and hooks. `.claude/skills/*`,
  `.claude/agents`, and `.claude/rules` are symlinks into it — edit under
  `.agents/`, never replace a link with a copy. Hooks wire via
  `.claude/settings.json`, not symlinks.
- Child apps stay standard Databricks Apps: never design anything that needs
  a child's `app.yaml` or source to change (constitution I).
- A child's route opens only after fetch → build env → start → health check
  succeed, and adding/updating one never interrupts running apps
  (constitution II, III).
- ADRs in `docs/decisions/` are append-only; reversing a decision = new ADR
  with supersession links.
- Docs update in the same change as the code that invalidates them.
- Don't present planned behavior as implemented — `ARCHITECTURE.md` marks
  what exists.

## Commands

- Spec Kit: `specify check` · feature flow `/speckit-specify` →
  `/speckit-plan` → `/speckit-tasks` → `/speckit-implement` →
  `/speckit-converge`
- Validate a skill after editing one:
  `uvx --from 'skills-ref==0.1.1' agentskills validate .agents/skills/<name>`
- Python gate (run all four before calling work done): `uv run ruff format --check .` ·
  `uv run ruff check .` · `uv run mypy src` · `uv run pytest tests/unit tests/contract tests/load`.
  Tests marked `integration` need a Databricks container and are skipped unless
  `APPMUX_TEST_CONTAINER=1`; they run from your machine against the deployed dev App through
  `scripts/run-remote-tests.sh` (the Databricks CLI's OAuth token is the caller).
- UI (`web/`, after `npm ci`; Node >= 22.18): `npm run typecheck` · `npm run lint` ·
  `npm run format:check` · `npm test` (unit + component) · `npm run build` (writes `web/out`) ·
  `npm run test:export` and `npm run test:e2e` (both read `web/out`, so run them after the
  build; the e2e run needs Playwright's Chromium); `npm run check` runs the first four.
  Run all seven before calling UI work done.
- Deploy scripts: `scripts/sync-deploy.sh` needs `APPMUX_WORKSPACE_FOLDER` and
  `APPMUX_DATABRICKS_PROFILE` (local, never committed, never defaulted); its output
  passes through `scripts/lib/mask-identifiers.sh`. Run `scripts/vendor-wheels.sh` and
  `scripts/fetch-slirp4netns.sh` first. For the integration tests, deploy with
  `APPMUX_DEPLOY_BOOTSTRAP_ADMIN` set to your own address (local, never committed: it replaces the
  placeholder admin in the synced copy of `app.yaml` only), so the token owner is a platform admin.
  The dev App also needs `workspace.workspace:read` in its `user_api_scopes` (Update reads the
  folder with the presser's token): `databricks apps update` replaces the whole body and takes
  no default scopes, and a changed scope reaches tokens only after a stop and start.

## Spec gate

Use Spec Kit only for work with ≥3 tasks / >2 days / changed contracts or a
debatable done condition — otherwise just make the change. Active feature:
[specs/001-appmux-v1/](specs/001-appmux-v1/spec.md) (appmux v1). The spikes
ran on 2026-10-30 on the Free Edition workspace only
([log](specs/001-appmux-v1/spikes/README.md)); ADR 0004 is `accepted` and the
[plan](specs/001-appmux-v1/plan.md) and [tasks](specs/001-appmux-v1/tasks.md)
exist. The owner asked for `/speckit-implement` on 2026-10-30; tasks are done in
waves, and `tasks.md` is ticked only for work whose tests pass.

## Docs map

| When you need to… | Read |
|---|---|
| Understand the structure / what is implemented | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Understand product intent, goals, non-goals | [docs/product/DESIGN.md](docs/product/DESIGN.md) |
| Know what v1 does, part by part | [docs/product/v1-design.md](docs/product/v1-design.md) · tables: [v1-data-model.md](docs/product/v1-data-model.md) · rationale and spikes: [v1-design-brief.md](docs/product/v1-design-brief.md) |
| Find active work | [specs/001-appmux-v1/](specs/001-appmux-v1/spec.md) · plan: [plan.md](specs/001-appmux-v1/plan.md) |
| Know what the Databricks Apps platform allows (container, proxy, identities, packages) | [docs/reference/databricks-apps-platform.md](docs/reference/databricks-apps-platform.md#platform-at-a-glance) · Free Edition limits: [databricks-free-edition.md](docs/reference/databricks-free-edition.md) |
| Understand why a technical choice was made | [docs/index.md](docs/index.md#decisions-adrs--append-only) |
| Know how the agent harness is laid out, wired, and updated | [docs/reference/agent-harness.md](docs/reference/agent-harness.md) |
| Understand the learned-context hooks | [.agents/hooks/learned/README.md](.agents/hooks/learned/README.md) (review via `/instinct-review`, graduation via `/evolve`) |
| Understand the security hooks and their limits | [.agents/hooks/security/README.md](.agents/hooks/security/README.md) |
| Check known debt/quirks | [docs/tech-debt.md](docs/tech-debt.md) |
| Standing principles for features | [.specify/memory/constitution.md](.specify/memory/constitution.md) |
