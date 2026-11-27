---
name: project-sync
description: Use when starting or finishing a unit of work in a project that tracks tasks, decisions, or experiments across external tools (e.g. Linear, Notion, Trackio, W&B). Before acting, check the project's configured sync channels for blockers, in-progress work, and recent decisions; after making changes, write status, decisions, and results back. Channels are read from per-project / global TOML config; tool- and project-agnostic; skip for trivial or unrelated tasks.
---

# Project Sync

Work task-management-first. This skill keeps execution aligned with the project's
external trackers and notes so you don't duplicate work, miss a blocker, or lose
a decision — and so experiment results and status land where the team looks.

## Sync channels (configurable)

Behavior is driven by **sync channels**, each a mapping of `purpose → tool → when`.
Do not compute the layered configuration yourself — read the same resolver the
SessionStart hook uses:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/project_sync_prompt.py" --effective
```

(Run from the project directory; in a source checkout of this repo the script is
`plugins/project-sync/hooks/project_sync_prompt.py`.) It prints one JSON document:

- `ok` / `errors` — validation is **fail-closed**: when `ok` is false, tell the
  user and do not guess destinations — never fall back to default channels.
- `channels` — the effective channels after layering: built-in defaults ← global
  `$XDG_CONFIG_HOME/project-sync/config.toml` ← project `.project-sync.toml`
  files (override by `purpose`, case-insensitive). `disabled` lists purposes
  turned off by config; never sync to them.
- `sources` — the config file paths that were read, so you can point the user at
  the exact file to edit.
- `project` — the explicit project binding (below).
- `worktree_root` — the git boundary used for config discovery (below).

Channel fields:

```toml
[[channel]]
purpose = "experiment results/logs"   # what you sync
tool    = "Trackio"                    # which tool / MCP (or its equivalent)
when    = ["after"]                    # "before" (check first), "after" (write back)
```

A channel can be turned off explicitly at any layer, including a built-in
default — only `purpose` is required on a disabled entry:

```toml
[[channel]]
purpose = "decisions / notes"
enabled = false
```

`when` must be a non-empty array of `"before"` / `"after"`; omit it for both
phases. An empty or mistyped `when` is a config error, not a fallback.

`tool` names a *kind* of destination — adapt to whatever MCP actually exists on
this machine (the Linear MCP tool names differ per host; infer, don't hardcode).

## Project binding and config discovery

A project config can bind the project explicitly, so tracker queries and writes
go straight to the right project instead of broad-searching by repository name:

```toml
[project]
name = "oss-sync"        # optional display name
linear = "OSS"           # optional Linear project name / ID
```

When `project.linear` (or `project.name`) is set in the `--effective` output,
use it directly.

Project configs are discovered from the cwd upward and **stop at the git
worktree root** — the nearest directory holding a `.git` entry (a directory in
a normal clone, a file in a linked worktree, so both locate identically).
Configs in parent directories above that root are ignored unless the nearest
project config opts in:

```toml
[project]
inherit_parent = true    # also merge .project-sync.toml files above the worktree root
```

The global config always lives at the XDG path and is unaffected by this
boundary.

## Before acting

1. Read the effective configuration (`--effective`, above). If `ok` is false,
   report the errors and stop — do not guess destinations.
2. For each enabled channel whose `when` includes `before` **and that is
   relevant to the task at hand**, consult that tool: check active blockers,
   in-progress issues, recent decisions / constraints. Do not poll every
   configured system unconditionally — the session hook is a low-frequency
   reminder, not a fan-out query.
3. Skip a channel if its tool has no usable MCP on this machine, and say so.

## After acting (incremental write-back)

Write back only what changed, and only to channels whose `when` includes
`after`:

1. **The tracker owns execution state.** Update the status of the *existing*
   issue for this work — search first (using the configured project binding),
   never file a duplicate. Create an issue only for genuinely new follow-up
   work.
2. **The repository owns numbers and artifacts.** Metrics, logs, and figures
   live in the repo; write back evidence links (commit, path, run URL), never
   copies of result tables or running ledgers.
3. **Notes write-back is opt-in.** A notes channel such as Notion is written
   only when its config includes `"after"` — and even then the phase only
   declares *when* to sync; user and project write restrictions still decide
   *whether* you may write. Record decisions and milestones, not transcripts.
4. **Report honestly.** If a channel's tool is unavailable or a write is
   declined, say "not synced: \<channel\>" in your final message instead of
   implying it happened.

Completion flows one way: from finished work to the tracker. This plugin
registers **no** `TaskCompleted` / `Stop` hook (only `SessionStart`), so
nothing updates the tracker automatically when a task completes — do not claim
otherwise. When the work has no tracker task object, the write-back path is
manual: summarize the outcome in your final message, and create or update an
issue only with explicit user approval.

## Scope

- Lightweight per-task discipline only.
- For multi-agent decomposition and checkpoint orchestration, use the heavier
  `notion-linear-agent-orchestrator` skill instead.