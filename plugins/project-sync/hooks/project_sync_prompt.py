#!/usr/bin/env python3
"""Low-frequency task-management context for Claude Code SessionStart.

The hook runs on startup, resume, explicit clear, and fork only. It never runs
for each user prompt or automatic compaction. Per-task before/after judgment
lives in the project-sync skill; this hook contributes one session-level
reminder.

Channels are configured via TOML (see resolve_channels). Configuration is
fail-closed: a damaged or invalid config file yields a stderr diagnostic and
no injected context, never a silent fallback to default channels.

CLI (both modes share the same resolver the skill reads):
  --check [dir]      human-readable validation; exit 1 on invalid config
  --effective [dir]  machine-readable JSON: effective channels, config source
                     paths, project binding, and errors; exit 1 on invalid

Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

Phase = Literal["before", "after"]


class Channel(TypedDict):
	"""One sync destination: a purpose mapped to a tool and its phases."""

	purpose: str
	tool: str
	when: list[Phase]
	enabled: NotRequired[bool]


DEFAULT_CHANNELS: list[Channel] = [
	{
		"purpose": "task plans",
		"tool": "Linear",
		"when": ["before", "after"],
		"enabled": True,
	},
	# Notion write-back ("after") is opt-in: the default only reads notes.
	{
		"purpose": "decisions / notes",
		"tool": "Notion",
		"when": ["before"],
		"enabled": True,
	},
]

GLOBAL_CONFIG_RELPATH = ("project-sync", "config.toml")
PROJECT_CONFIG_NAME = ".project-sync.toml"
SESSION_SOURCES: set[str] = {"startup", "resume", "clear", "fork"}
USAGE = "usage: project_sync_prompt.py [--check|--effective [dir]]"


class ConfigError(Exception):
	"""A config file exists but cannot be read or violates the schema."""


@dataclass
class ProjectBinding:
	"""Explicit project identity from [project] tables; nearest file wins."""

	name: str | None = None
	linear: str | None = None
	inherit_parent: bool = False
	source: str | None = None


# --- channel config ------------------------------------------------------------


def _normalize_when(raw: Mapping[str, object]) -> list[Phase]:
	"""Return the phase list; an omitted `when` defaults to both phases.

	An explicit empty, mistyped, or unknown-phase `when` is a ConfigError,
	never a silent fallback to before/after.
	"""
	if "when" not in raw:
		return ["before", "after"]
	when = raw["when"]
	if not isinstance(when, list):
		raise ConfigError('when must be an array of "before"/"after" entries')
	if not when:
		raise ConfigError("when must not be empty; set enabled = false to disable")
	phases: list[Phase] = []
	for item in when:
		if item == "before" or item == "after":
			if item not in phases:
				phases.append(item)
		else:
			raise ConfigError(f'when entries must be "before" or "after", got {item!r}')
	return phases


def normalize_channel(raw: Mapping[str, object]) -> Channel:
	"""Validate a raw config table into a Channel, raising ConfigError."""
	enabled = raw.get("enabled", True)
	if not isinstance(enabled, bool):
		raise ConfigError("enabled must be a boolean")
	purpose = raw.get("purpose")
	if not isinstance(purpose, str) or not purpose.strip():
		raise ConfigError("purpose must be a non-empty string")
	purpose = purpose.strip()
	if not enabled:
		# A disable directive only needs its purpose; tool/when are ignored.
		return {"purpose": purpose, "tool": "", "when": [], "enabled": False}
	tool = raw.get("tool")
	if not isinstance(tool, str) or not tool.strip():
		raise ConfigError("tool must be a non-empty string")
	return {
		"purpose": purpose,
		"tool": tool.strip(),
		"when": _normalize_when(raw),
		"enabled": True,
	}


def _copy_channel(channel: Channel) -> Channel:
	return {
		"purpose": channel["purpose"],
		"tool": channel["tool"],
		"when": list(channel["when"]),
		"enabled": channel.get("enabled", True),
	}


def merge_channels(base: list[Channel], override: list[Channel]) -> list[Channel]:
	"""Override base by purpose (case-insensitive); append unseen purposes."""
	merged = [_copy_channel(c) for c in base]
	index = {c["purpose"].lower(): i for i, c in enumerate(merged)}
	for c in override:
		key = c["purpose"].lower()
		if key in index:
			merged[index[key]] = _copy_channel(c)
		else:
			index[key] = len(merged)
			merged.append(_copy_channel(c))
	return merged


def _project_from(raw: object) -> ProjectBinding:
	"""Validate a raw [project] table, raising ConfigError on bad shapes."""
	if raw is None:
		return ProjectBinding()
	if not isinstance(raw, dict):
		raise ConfigError("'project' must be a table ([project])")
	binding = ProjectBinding()
	name = raw.get("name")
	if name is not None:
		if not isinstance(name, str) or not name.strip():
			raise ConfigError("project.name must be a non-empty string")
		binding.name = name.strip()
	linear = raw.get("linear")
	if linear is not None:
		if not isinstance(linear, str) or not linear.strip():
			raise ConfigError("project.linear must be a non-empty string")
		binding.linear = linear.strip()
	inherit = raw.get("inherit_parent", False)
	if not isinstance(inherit, bool):
		raise ConfigError("project.inherit_parent must be a boolean")
	binding.inherit_parent = inherit
	return binding


# --- config discovery ------------------------------------------------------------


def find_worktree_root(start_dir: str) -> str | None:
	"""Return the nearest ancestor holding a .git entry, or None.

	A normal clone has a .git directory at its root; a linked worktree (or a
	submodule) has a .git file, so both shapes mark the same boundary.
	"""
	cur = os.path.realpath(start_dir)
	while True:
		if os.path.exists(os.path.join(cur, ".git")):
			return cur
		parent = os.path.dirname(cur)
		if parent == cur:
			return None
		cur = parent


def _collect_configs(start_dir: str, stop: str | None) -> list[str]:
	"""Collect config files walking up from start_dir, nearest first.

	When stop is given the walk ends there (inclusive); otherwise it ends at
	the filesystem root.
	"""
	found: list[str] = []
	cur = os.path.realpath(start_dir)
	while True:
		candidate = os.path.join(cur, PROJECT_CONFIG_NAME)
		if os.path.isfile(candidate):
			found.append(candidate)
		if stop is not None and cur == stop:
			break
		parent = os.path.dirname(cur)
		if parent == cur:
			break
		cur = parent
	return found


def find_project_configs(start_dir: str) -> tuple[list[str], str | None]:
	"""Locate project config files (nearest first) and the worktree boundary.

	The search ascends from start_dir and stops at the git worktree root, so a
	normal clone and a linked worktree resolve identically and unrelated parent
	directories never leak in. Without a .git boundary it ascends to the
	filesystem root. Configs above the boundary join only when the nearest
	config opts in via [project] inherit_parent (see resolve_channels).
	"""
	boundary = find_worktree_root(start_dir)
	return _collect_configs(start_dir, boundary), boundary


def load_toml(path: str) -> dict[str, object] | None:
	"""Parse a TOML file; None when absent, ConfigError when damaged."""
	try:
		with open(path, "rb") as f:
			return tomllib.load(f)
	except FileNotFoundError:
		return None
	except PermissionError as exc:
		raise ConfigError("config file is not readable (permission denied)") from exc
	except tomllib.TOMLDecodeError as exc:
		raise ConfigError(f"invalid TOML: {exc}") from exc
	except UnicodeDecodeError as exc:
		raise ConfigError("invalid TOML: file is not valid UTF-8") from exc
	except OSError as exc:
		raise ConfigError(f"config file cannot be read: {exc}") from exc


def _load_file(path: str) -> tuple[list[Channel], ProjectBinding]:
	"""Parse one config file into its channels and [project] binding."""
	data = load_toml(path)
	if data is None:
		return [], ProjectBinding()
	raw_channels = data.get("channel", [])
	if not isinstance(raw_channels, list):
		raise ConfigError("'channel' must be an array of tables ([[channel]])")
	out: list[Channel] = []
	for i, raw in enumerate(raw_channels):
		if not isinstance(raw, dict):
			raise ConfigError(f"channel[{i}] must be a table")
		try:
			out.append(normalize_channel(raw))
		except ConfigError as exc:
			raise ConfigError(f"channel[{i}]: {exc}") from exc
	return out, _project_from(data.get("project"))


# --- resolution ----------------------------------------------------------------


@dataclass
class Resolution:
	"""Outcome of composing channel layers.

	When errors is non-empty the configuration is invalid and channels is
	empty: callers must fail closed rather than fall back to defaults.
	"""

	channels: list[Channel]
	errors: list[str]
	disabled: list[str] = field(default_factory=list)
	project: ProjectBinding = field(default_factory=ProjectBinding)
	global_path: str = ""
	project_paths: list[str] = field(default_factory=list)
	worktree_root: str | None = None


def resolve_channels(
	cwd: str,
	home: str,
	env: Mapping[str, str] | None = None,
) -> Resolution:
	"""Compose channels: built-in default <- global config <- project configs.

	Project configs are discovered within the git worktree boundary (nearest
	first); the nearest one may opt into inheriting parent-directory configs
	via [project] inherit_parent = true. [project] bindings are honored from
	project files only; a global [project] table is validated but not applied.
	"""
	environment: Mapping[str, str] = os.environ if env is None else env
	xdg = environment.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
	global_path = os.path.join(xdg, *GLOBAL_CONFIG_RELPATH)

	errors: list[str] = []
	global_channels: list[Channel] = []
	try:
		global_channels, _ = _load_file(global_path)
	except ConfigError as exc:
		errors.append(f"{global_path}: {exc}")

	within, boundary = find_project_configs(cwd)
	project_paths = list(within)
	parsed: list[tuple[str, list[Channel], ProjectBinding]] = []

	def load_project(path: str) -> ProjectBinding | None:
		try:
			channels, binding = _load_file(path)
		except ConfigError as exc:
			errors.append(f"{path}: {exc}")
			return None
		parsed.append((path, channels, binding))
		return binding

	if within:
		nearest = load_project(within[0])
		if nearest is not None and nearest.inherit_parent and boundary is not None:
			project_paths += _collect_configs(os.path.dirname(boundary), None)
		for path in project_paths[1:]:
			load_project(path)

	if errors:
		return Resolution(
			channels=[],
			errors=errors,
			global_path=global_path,
			project_paths=project_paths,
			worktree_root=boundary,
		)

	channels = [_copy_channel(c) for c in DEFAULT_CHANNELS]
	channels = merge_channels(channels, global_channels)
	project = ProjectBinding()
	for path, layer, binding in reversed(parsed):
		channels = merge_channels(channels, layer)
		if binding.name is not None:
			project.name = binding.name
			project.source = path
		if binding.linear is not None:
			project.linear = binding.linear
			project.source = path
	if parsed:
		project.inherit_parent = parsed[0][2].inherit_parent

	disabled = [c["purpose"] for c in channels if not c.get("enabled", True)]
	enabled = [c for c in channels if c.get("enabled", True)]
	return Resolution(
		channels=enabled,
		errors=[],
		disabled=disabled,
		project=project,
		global_path=global_path,
		project_paths=project_paths,
		worktree_root=boundary,
	)


# --- session reminder ----------------------------------------------------------


def build_session_text(
	channels: list[Channel],
	project: ProjectBinding | None = None,
) -> str:
	"""Render one compact reminder for the whole session."""
	before = [c for c in channels if "before" in c["when"]]
	after = [c for c in channels if "after" in c["when"]]
	parts = ["Project-sync session reminder for non-trivial tracked-project work."]
	if before:
		checks = "; ".join(f"{c['purpose']} in {c['tool']}" for c in before)
		parts.append(
			f"Before acting on such work, check the task-relevant channels: {checks}."
		)
	if after:
		targets = "; ".join(f"{c['purpose']} in {c['tool']}" for c in after)
		parts.append(
			"Before finishing material work, use the project-sync skill to update "
			f"only changed project facts in the matching after channels: {targets}."
		)
	parts.append(
		"Channel phases declare when to sync; they never grant write access, "
		"so user and project write restrictions always apply."
	)
	if project is not None and (project.name is not None or project.linear is not None):
		bits = []
		if project.name is not None:
			bits.append(f"name {project.name!r}")
		if project.linear is not None:
			bits.append(f"Linear project {project.linear!r}")
		source = f", from {project.source}" if project.source else ""
		parts.append(
			f"Configured project binding ({', '.join(bits)}{source}); "
			"use it directly instead of searching by repository name."
		)
	else:
		parts.append(
			"Infer the project from cwd or git remote, use the first configured "
			"available integration, and skip trivial or unrelated work."
		)
	return " ".join(parts)


def should_emit(event: str, source: str | None) -> bool:
	"""Return whether this invocation is an intended low-frequency session start."""
	return event == "SessionStart" and source in SESSION_SOURCES


# --- entry points ----------------------------------------------------------------


def effective_payload(resolution: Resolution, cwd: str) -> dict[str, object]:
	"""Machine-readable effective configuration shared by hook and skill."""
	return {
		"version": 1,
		"ok": not resolution.errors,
		"cwd": os.path.realpath(cwd),
		"worktree_root": resolution.worktree_root,
		"sources": {
			"global": {
				"path": resolution.global_path,
				"exists": os.path.isfile(resolution.global_path),
			},
			"project": list(resolution.project_paths),
		},
		"project": {
			"name": resolution.project.name,
			"linear": resolution.project.linear,
			"inherit_parent": resolution.project.inherit_parent,
			"source": resolution.project.source,
		},
		"channels": [
			{
				"purpose": c["purpose"],
				"tool": c["tool"],
				"when": list(c["when"]),
			}
			for c in resolution.channels
		],
		"disabled": list(resolution.disabled),
		"errors": list(resolution.errors),
	}


def check_config(cwd: str, home: str, env: Mapping[str, str] | None = None) -> int:
	"""Validate the effective configuration; return 0 when usable, 1 otherwise."""
	resolution = resolve_channels(cwd=cwd, home=home, env=env)
	for error in resolution.errors:
		print(f"project-sync: {error}", file=sys.stderr)
	if resolution.errors:
		return 1
	if not resolution.channels:
		print("project-sync: all channels are disabled")
		return 0
	for channel in resolution.channels:
		phases = ", ".join(channel["when"])
		print(f"{channel['purpose']} -> {channel['tool']} [{phases}]")
	binding = []
	if resolution.project.name is not None:
		binding.append(resolution.project.name)
	if resolution.project.linear is not None:
		binding.append(f"linear: {resolution.project.linear}")
	if binding:
		print(f"project: {', '.join(binding)}")
	return 0


def effective_config(cwd: str, home: str, env: Mapping[str, str] | None = None) -> int:
	"""Print the effective configuration as JSON; return 0 when usable, 1 otherwise."""
	resolution = resolve_channels(cwd=cwd, home=home, env=env)
	for error in resolution.errors:
		print(f"project-sync: {error}", file=sys.stderr)
	print(json.dumps(effective_payload(resolution, cwd), indent=2))
	return 1 if resolution.errors else 0


def main(argv: list[str] | None = None) -> int:
	args = list(sys.argv[1:] if argv is None else argv)
	if args and args[0] in ("--check", "--effective"):
		if len(args) > 2:
			print(USAGE, file=sys.stderr)
			return 2
		target = args[1] if len(args) == 2 else os.getcwd()
		if args[0] == "--check":
			return check_config(cwd=target, home=os.path.expanduser("~"))
		return effective_config(cwd=target, home=os.path.expanduser("~"))
	if args:
		print(USAGE, file=sys.stderr)
		return 2

	try:
		data = json.load(sys.stdin)
	except Exception:
		return 0
	if not isinstance(data, dict):
		return 0

	event = str(data.get("hook_event_name") or "")
	source_value = data.get("source")
	source = str(source_value) if source_value is not None else None
	if not should_emit(event, source):
		return 0

	cwd_value = data.get("cwd")
	cwd = str(cwd_value) if cwd_value else os.getcwd()
	resolution = resolve_channels(cwd=cwd, home=os.path.expanduser("~"))
	if resolution.errors:
		for error in resolution.errors:
			print(f"project-sync: {error}", file=sys.stderr)
		return 0
	if not resolution.channels:
		return 0

	print(
		json.dumps(
			{
				"hookSpecificOutput": {
					"hookEventName": "SessionStart",
					"additionalContext": build_session_text(
						resolution.channels, resolution.project
					),
				}
			}
		)
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
