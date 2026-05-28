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
Read the configuration before acting (first match wins, then merge):

1. **Per-project:** nearest `.project-sync.toml` walking up from the cwd to the
   repo / filesystem root.
2. **Global:** `$XDG_CONFIG_HOME/project-sync/config.toml` (default
   `~/.config/project-sync/config.toml`).

Per-project channels override global channels with the same `purpose`
(case-insensitive) and add new ones. If no config exists, assume the default:
`task plans → Linear` and `decisions / notes → Notion`, both before and after.

Each channel:

```toml
[[channel]]
purpose = "experiment results/logs"   # what you sync
tool    = "Trackio"                    # which tool / MCP (or its equivalent)
when    = ["after"]                    # "before" (check first), "after" (write back)
```

`tool` names a *kind* of destination — adapt to whatever MCP actually exists on
this machine (the Linear MCP tool names differ per host; infer, don't hardcode).

## Before acting

1. Identify the project from cwd / git remote.
2. Read the sync-channel config (above) to learn this project's channels.
3. For each channel whose `when` includes `before`, consult that tool: check
   active blockers, in-progress issues, recent decisions / constraints relevant
   to the task at hand.
4. Skip a channel if its tool has no usable MCP on this machine.

## After acting

For each channel whose `when` includes `after`, reflect what changed:
1. Update the corresponding tracker issue(s) status.
2. Record decisions / milestones in the project notes.
3. Log experiment results / metrics to the experiment channel.
4. If follow-ups emerged, create tracker issues for them.

## Scope

- Lightweight per-task discipline only.
- For multi-agent decomposition and checkpoint orchestration, use the heavier
  `notion-linear-agent-orchestrator` skill instead.
