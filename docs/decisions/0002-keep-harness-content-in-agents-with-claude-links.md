# Keep harness content in `.agents/` and link it into `.claude/`

**Status:** accepted
**Date:** 2026-10-29
**Deciders:** Liam Lee

## Context

appmux uses the agent-coding harness from `timezlab/vibe`: skills, subagent
definitions, always-on rule packs, and security/learning hooks. Claude Code
loads these from `.claude/`. Other agents read skills from `.agents/skills`
(per the platform matrix in vibe's `docs/reference/platform-directories.md`,
verified 2026-07-23: Codex, Gemini CLI, and Cursor all do). Spec Kit's Claude
integration, left alone, writes its skills straight into `.claude/skills`.

## Decision

The real files live under `.agents/` — `.agents/skills/<name>/`,
`.agents/agents/`, `.agents/rules/`, `.agents/hooks/`. `.claude/` holds
symlinks only: one per skill (`.claude/skills/<name>` →
`../../.agents/skills/<name>`), and whole-directory links for
`.claude/agents` and `.claude/rules`. Hooks are not linked; they are wired in
`.claude/settings.json` with commands that point at `.agents/hooks/`. The
vibe content is copied from a pinned `timezlab/vibe` commit with
path-reference rewrites only (listed in
[agent-harness.md](../reference/agent-harness.md#local-changes-to-upstream)).
The Spec Kit skills are moved into `.agents/skills/` and linked back, so the
integration manifest's `.claude/skills/...` paths still resolve.

## Alternatives considered

- **Real files in `.claude/`** — rejected: agents that read `.agents/skills`
  would need a second copy, and two copies drift.
- **Keep vibe's root layout (`skills/`, `rules/`, `hooks/`, `agents/`) with
  `.agents` linking to it** — rejected: that layout exists because vibe
  authors the content; here it is consumed, and four more top-level
  directories would clutter the product repository.
- **`npx skills add` from vibe** — rejected as the only mechanism: it installs
  skills, not rules, agents, or hooks.

## Consequences

**Better:**

- One copy of every skill, readable by every agent that follows the
  `.agents/skills` convention.
- Everything harness-related is under one dot-directory, away from product
  code.

**Worse:**

- A symlink committed to git becomes a plain text file on a Windows checkout
  without `core.symlinks`, silently breaking Claude Code's view of the harness
  for that contributor.
- Rules, agents, and hooks are still Claude Code–native; living under
  `.agents/` does not make other agents load them.
- Upstream vibe updates are a manual re-copy plus re-applying the path
  rewrites ([TD-001](../tech-debt.md#td-001-harness-updates-are-manual)).

**Must now be true:**

- Harness content is edited under `.agents/`; `.claude/skills/*`,
  `.claude/agents`, and `.claude/rules` stay symlinks, never copies.
- Hook commands in `.claude/settings.json` use
  `$CLAUDE_PROJECT_DIR/.agents/hooks/...`.
- The `evolve` and `instinct-review` skills call
  `.agents/hooks/learned/lib/learned_store.py`; a bare `hooks/learned/...`
  path does not exist here.

## Revisit if

Claude Code reads `.agents/` natively, `vibe sync` ships and takes over
placement, or Windows contributors hit broken links often enough to justify
copies.
