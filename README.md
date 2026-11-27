# project-sync

A portable Claude Code **plugin** that instills a *task-management-first* habit:
before you act, check the project's tracker and notes; after you change state,
write the result back. Tool- and project-agnostic, configurable per project, and
installable across **Claude Code**, **Codex CLI**, and **Copilot CLI**.

This repo is a **git-repo marketplace** hosting one plugin (`project-sync`).

## What it does

The plugin ships two complementary pieces:

| Piece | Mechanism | Behavior |
|-------|-----------|----------|
| **Hook** (`SessionStart`) | Fires on `startup`, `resume`, `clear`, and `fork`; never on per-prompt events or automatic compaction | Injects one compact session reminder covering both the initial check and eventual write-back. |
| **Skill** (`project-sync`) | **Model-invoked** when the task matches | The per-task *before/after* playbook — loaded when the model judges it's starting or finishing a unit of work. |

**Why this split?** Starting and finishing a meaningful unit of work are semantic
judgments, so the model-invoked skill owns both. The hook only makes that policy
available once when a session starts or is explicitly reset. There is no
`UserPromptSubmit` or `Stop` hook, so Project Sync adds no per-turn process or
repeated reminder.

Only a *plugin* can register always-on hooks; the skill is also the graceful
fallback on CLIs where hook context-injection isn't honored (see
[Cross-CLI support](#cross-cli-support)).

## Sync channels (configuration)

Behavior is driven by **sync channels** — each maps a `purpose` to a `tool` and
the phase(s) it applies to (`before` / `after`). Config is layered, later
sources overriding earlier ones by `purpose` (case-insensitive):

1. **Built-in default:** `task plans → Linear` (`before` + `after`) and
   `decisions / notes → Notion` (`before` only — Notion write-back is opt-in).
2. **Global:** `$XDG_CONFIG_HOME/project-sync/config.toml`
   (default `~/.config/project-sync/config.toml`).
3. **Per-project:** `.project-sync.toml` files from the cwd up to the **git
   worktree root** — the nearest directory holding a `.git` entry (a directory
   in a normal clone, a file in a linked worktree, so both locate identically).
   Configs in parent directories above that root are ignored unless the nearest
   project config opts in with `[project] inherit_parent = true`.

```toml
# .project-sync.toml — extends/overrides the global + default channels
[[channel]]
purpose = "task plans"
tool    = "Linear"
when    = ["before", "after"]

[[channel]]
purpose = "experiment results/logs"
tool    = "Trackio"
when    = ["after"]            # write-back only

[[channel]]
purpose = "decisions / notes"
enabled = false                # explicitly disable a channel (e.g. a default)
```

Configuration rules:

- `enabled = false` disables a channel at any layer, including built-in
  defaults; a disabled entry needs only its `purpose`.
- `when` must be a non-empty array of `"before"` / `"after"`. An omitted
  `when` defaults to both phases, but an empty or mistyped `when` is an
  error — never a silent fallback.
- `purpose` and `tool` must be non-empty strings (`tool` may be omitted only
  when `enabled = false`).
- Validation is **fail-closed**: a damaged or unreadable config file (broken
  TOML, bad schema, missing permissions) makes the hook print a stderr
  diagnostic and inject nothing, instead of silently restoring default
  channels. Validate with
  `python3 plugins/project-sync/hooks/project_sync_prompt.py --check`
  (exits non-zero on invalid config).
- A configured `after` phase only declares *when* to sync; it never grants
  write access. User and project write restrictions always apply.
- A project file may bind the project explicitly with a `[project]` table
  (`name`, `linear`), so agents query the tracker directly instead of
  broad-searching by repository name. `[project]` is honored from project
  files only.
- Hook and skill share one resolver:
  `python3 plugins/project-sync/hooks/project_sync_prompt.py --effective`
  prints the effective channels, config source paths, project binding, and
  worktree boundary as JSON (exit 1 when invalid), so the skill never
  re-derives the layering.

`tool` names a *kind* of destination. The agent adapts to whatever MCP actually
exists on the machine — the hook never hardcodes per-host MCP tool names. See
[`project-sync.example.toml`](./project-sync.example.toml) for a full example.

## Install

Once this repo is on GitHub (e.g. `Acture/project_sync` — confirm your slug):

| CLI | How to register |
|-----|-----------------|
| **Claude Code** | `claude plugin marketplace add Acture/project_sync`, then `/plugin` → enable `project-sync`. Or edit `~/.claude/settings.json`: add the repo to `extraKnownMarketplaces` and `"project-sync@claude-project-sync": true` to `enabledPlugins`. |
| **Codex** | Edit `~/.codex/config.toml`: add a `[marketplaces.claude-project-sync]` git source and `[plugins."project-sync@claude-project-sync"] enabled = true`. |
| **Copilot** | Edit `~/.copilot/settings.json`: add `"project-sync@claude-project-sync": true` to `enabledPlugins`. |

For local testing before pushing:
`claude plugin marketplace add ~/repos/project_sync` (point at this path).

## Cross-CLI support

- **Claude Code** — hook auto-fires *and* skill is model-invocable. ✅ full.
- **Codex / Copilot** — the hook **runs** (proven cross-CLI), but whether its
  `additionalContext` injection is honored is **CLI-confirmed only on Claude
  Code**. On those CLIs, treat the **skill** as the reliable path; the hook is a
  best-effort bonus.

Minimum bar on all three CLIs: the skill is model-invocable.

## Development

The hook is stdlib-only Python 3.11+ (no dependencies), so plain `python3`
runs it on macOS / Linux / WSL.

```sh
# Run the test suite
python3 -m unittest discover -s tests -v

# Smoke-test the low-frequency SessionStart reminder
echo '{"hook_event_name":"SessionStart","source":"startup"}' | python3 plugins/project-sync/hooks/project_sync_prompt.py

# Validate the effective channel configuration for a directory (exit 1 on error)
python3 plugins/project-sync/hooks/project_sync_prompt.py --check

# Print the effective configuration as JSON (channels, sources, project binding)
python3 plugins/project-sync/hooks/project_sync_prompt.py --effective

# Quality gates
ruff check .
ruff format --check .
ty check .
```

## Layout

```
project_sync/
  .claude-plugin/marketplace.json        # marketplace manifest
  project-sync.example.toml              # example channel config
  pyproject.toml                         # ty extra-search-path for the hook module
  plugins/project-sync/
    .claude-plugin/plugin.json
    hooks/
      hooks.json                         # registers filtered SessionStart only
      project_sync_prompt.py             # stdlib-only, config-driven session reminder
    skills/project-sync/SKILL.md
  tests/test_project_sync_prompt.py
```
