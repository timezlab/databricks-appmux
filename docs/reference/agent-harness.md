# Agent harness

How the coding-agent harness is laid out, wired, and updated in this
repository. Decision and rationale:
[ADR 0002](../decisions/0002-keep-harness-content-in-agents-with-claude-links.md).

**Source:** [`timezlab/vibe`](https://github.com/timezlab/vibe) at commit
`1192fda5b97b460e326a2cc99e28224ba71242a4` (skills, agents, rules, hooks) ·
GitHub Spec Kit `0.13.4` (`specify init . --integration claude --script sh`).

## Layout

Real files live under `.agents/`; `.claude/` holds links.

| Content | Real location | Linked from |
|---|---|---|
| Skills | `.agents/skills/<name>/` | `.claude/skills/<name>` (one link per skill) |
| Subagents | `.agents/agents/<lane>/<name>.md` | `.claude/agents` (directory link) |
| Rule packs | `.agents/rules/{common,typescript,python,shell}/` | `.claude/rules` (directory link) |
| Hooks | `.agents/hooks/{security,learned}/` | not linked — wired in `.claude/settings.json` |

## What is installed

| Kind | Items |
|---|---|
| vibe skills | `brainstorming` · `coding-style` · `debugging` · `docs-architect` · `evolve` · `git-workflow` · `instinct-review` · `parallel-agents` · `prompt-guru` · `research` · `security-audit` · `tdd` |
| Spec Kit skills | `speckit-constitution` · `-specify` · `-clarify` · `-plan` · `-checklist` · `-tasks` · `-analyze` · `-implement` · `-converge` · `-taskstoissues` |
| Subagents | `research/{web-researcher,academic-researcher,code-researcher,fact-checker}` · `security/security-reviewer` · `debugging/root-cause-analyst` |
| Rule packs | `common` (coding-style, security, agent-behavior) · `typescript`, `python`, `shell` (coding-style, security; each loads only for matching file globs) |
| Hooks | `security/` (3 PreToolUse guards) · `learned/` (SessionStart loader, SessionEnd capture) |

Not copied from vibe: its `tests/`, `pytest.ini`, CI workflow,
`skills-lock.json` and vendored `skill-creator` skill, `brand/`, and its own
docs, specs, and constitution.

## Hook wiring

All five are in `.claude/settings.json`, as `python3
"$CLAUDE_PROJECT_DIR/.agents/hooks/<pack>/<script>"`.

| Event · matcher | Script | Effect |
|---|---|---|
| PreToolUse · `Bash` | `security/block-dangerous-commands.py` | denies destructive/exfiltrating shell shapes |
| PreToolUse · `Bash` | `security/dependency-gate.py` | denies installs of OSV `MAL-` versions; asks on young/unknown/unverifiable packages (needs network on install commands) |
| PreToolUse · `Read\|Edit\|Write\|NotebookEdit` | `security/protect-secrets.py` | denies secret paths and writes containing live credential shapes |
| SessionStart · `startup\|clear\|compact` | `learned/load-learned-context.py` | injects approved instincts + latest session summary |
| SessionEnd | `learned/capture-learned-context.py` | writes pending instinct candidates |

Things to know:

- The security hooks are tripwires, not a sandbox — limits are in
  [`.agents/hooks/security/README.md`](../../.agents/hooks/security/README.md).
- `capture-learned-context.py` runs one headless `claude -p` extraction
  (haiku, capped at 120 s) at the end of sessions with 10 or more user
  messages, so it spends Claude usage. To stop it, delete the `SessionEnd`
  entry; to stop loading lessons, delete the `SessionStart` entry. Details:
  [`.agents/hooks/learned/README.md`](../../.agents/hooks/learned/README.md).
- The learned store is `<repo>/.vibe/learned/`. In a git repository the hooks
  exclude it through `.git/info/exclude`; outside one they print `non-git
  fallback` and use the same path without excluding it.

## Local changes to upstream

Only Markdown was edited; every hook, script, and rule body is byte-identical
to upstream. The edits make vibe-root paths resolve in this layout:

| Rewrite | Why |
|---|---|
| `hooks/…`, `rules/<pack>`, `agents/<lane>/`, `skills/<name>` → `.agents/…` | upstream prose and commands assume vibe's repo-root layout; `evolve` and `instinct-review` run `python3 .agents/hooks/learned/lib/learned_store.py` |
| `$CLAUDE_PROJECT_DIR/hooks/…` → `$CLAUDE_PROJECT_DIR/.agents/hooks/…` | hook wiring examples in the hook READMEs |
| relative links to vibe's `docs/decisions/*`, `ARCHITECTURE.md`, `CONTRIBUTING.md` → GitHub permalinks at the pinned commit | those files do not exist here |

Files touched: `skills/{coding-style,evolve,instinct-review,research}/SKILL.md`,
`skills/security-audit/{SKILL.md,MAINTENANCE.md,references/agent-harness.md}`,
`rules/{README.md,common/security.md}`,
`hooks/{learned,security}/README.md` (all under `.agents/`).

## Updating from upstream

Manual until vibe ships `vibe sync` ([TD-001](../tech-debt.md#td-001-harness-updates-are-manual)).

1. Clone `timezlab/vibe` at the target ref outside this repository.
2. Copy its `skills/`, `agents/`, `rules/`, `hooks/` over the matching
   `.agents/` directories; add a `.claude/skills/<name>` link for any new skill.
3. Re-apply the rewrites above, then check nothing was missed:
   `grep -rnE '(^|[^A-Za-z0-9_./-])hooks/(security|learned)/' .agents --include='*.md'`
   must print nothing, and `python3 .agents/hooks/learned/lib/learned_store.py check`
   must print `store ok`.
4. Re-run the security hook checks in
   [`.agents/hooks/security/README.md`](../../.agents/hooks/security/README.md#testing-a-change).
5. Update the pinned commit at the top of this file.
