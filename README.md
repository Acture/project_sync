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
| **Hook** (`UserPromptSubmit`) | Fires **automatically** every prompt | Injects a reminder to consult your *sync channels* first; if the previous turn made state changes (edits, commits, issue saves, …), appends a "sync those changes back" nudge. |
| **Skill** (`project-sync`) | **Model-invoked** when the task matches | The portable reminder core — a before/after playbook the model loads when starting or finishing a unit of work. |

**Why both?** A skill is loaded only when the model judges it relevant; only a
*plugin* can register a hook that fires unconditionally. The hook is the
auto-fire path; the skill is the graceful fallback on CLIs where hook
context-injection isn't honored (see [Cross-CLI support](#cross-cli-support)).

## Sync channels (configuration)

Behavior is driven by **sync channels** — each maps a `purpose` to a `tool` and
the phase(s) it applies to (`before` / `after`). Config is layered, later
sources overriding earlier ones by `purpose` (case-insensitive):

1. **Built-in default:** `task plans → Linear` and `decisions / notes → Notion`,
   both `before` and `after`.
2. **Global:** `$XDG_CONFIG_HOME/project-sync/config.toml`
   (default `~/.config/project-sync/config.toml`).
3. **Per-project:** nearest `.project-sync.toml` walking up from the cwd.

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
```

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

The hook is stdlib-only Python (no dependencies), so plain `python3` runs it on
macOS / Linux / WSL.

```sh
# Run the test suite
python3 -m unittest discover -s tests -v

# Smoke-test the hook directly
echo '{"transcript_path":"/tmp/none"}' | python3 plugins/project-sync/hooks/project_sync_prompt.py
```

## Layout

```
project_sync/
  .claude-plugin/marketplace.json        # marketplace manifest
  project-sync.example.toml              # example channel config
  plugins/project-sync/
    .claude-plugin/plugin.json
    hooks/
      hooks.json                         # registers the UserPromptSubmit hook
      project_sync_prompt.py             # stdlib-only, config-driven
    skills/project-sync/SKILL.md
  tests/test_project_sync_prompt.py
```
