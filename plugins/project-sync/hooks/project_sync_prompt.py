#!/usr/bin/env python3
"""Low-frequency task-management context for Claude Code SessionStart.

The hook runs on startup, resume, and explicit clear only. It never runs for
each user prompt or automatic compaction. Per-task before/after judgment lives
in the project-sync skill; this hook contributes one session-level reminder.

Channels are configured via TOML (see resolve_channels). Configuration is
fail-closed: a damaged or invalid config file yields a stderr diagnostic and
no injected context, never a silent fallback to default channels. Run with
--check to validate the effective configuration for a directory.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
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
SESSION_SOURCES: set[str] = {"startup", "resume", "clear"}


class ConfigError(Exception):
	"""A config file exists but cannot be read or violates the schema."""


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


def find_config_file(start_dir: str, filename: str) -> str | None:
	"""Walk up from start_dir to the filesystem root, returning the first match."""
	cur = os.path.abspath(start_dir)
	while True:
		candidate = os.path.join(cur, filename)
		if os.path.isfile(candidate):
			return candidate
		parent = os.path.dirname(cur)
		if parent == cur:
			return None
		cur = parent


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


def _channels_from(path: str | None) -> list[Channel]:
	if not path:
		return []
	data = load_toml(path)
	if data is None:
		return []
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
	return out


@dataclass
class Resolution:
	"""Outcome of composing channel layers.

	When errors is non-empty the configuration is invalid and channels is
	empty: callers must fail closed rather than fall back to defaults.
	"""

	channels: list[Channel]
	errors: list[str]


def resolve_channels(
	cwd: str,
	home: str,
	env: Mapping[str, str] | None = None,
) -> Resolution:
	"""Compose channels: built-in default <- global config <- per-project config."""
	environment: Mapping[str, str] = os.environ if env is None else env
	xdg = environment.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
	global_path = os.path.join(xdg, *GLOBAL_CONFIG_RELPATH)
	project_path = find_config_file(cwd, PROJECT_CONFIG_NAME)

	errors: list[str] = []
	layers: list[list[Channel]] = []
	for path in (global_path, project_path):
		try:
			layers.append(_channels_from(path))
		except ConfigError as exc:
			errors.append(f"{path}: {exc}")
	if errors:
		return Resolution(channels=[], errors=errors)

	channels = [_copy_channel(c) for c in DEFAULT_CHANNELS]
	for layer in layers:
		channels = merge_channels(channels, layer)
	enabled = [c for c in channels if c.get("enabled", True)]
	return Resolution(channels=enabled, errors=[])


# --- session reminder ----------------------------------------------------------


def build_session_text(channels: list[Channel]) -> str:
	"""Render one compact reminder for the whole session."""
	before = [c for c in channels if "before" in c["when"]]
	after = [c for c in channels if "after" in c["when"]]
	parts = ["Project-sync session reminder for non-trivial tracked-project work."]
	if before:
		checks = "; ".join(f"{c['purpose']} in {c['tool']}" for c in before)
		parts.append(f"Before acting, check {checks}.")
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
	parts.append(
		"Infer the project from cwd or git remote, use the first configured "
		"available integration, and skip trivial or unrelated work."
	)
	return " ".join(parts)


def should_emit(event: str, source: str | None) -> bool:
	"""Return whether this invocation is an intended low-frequency session start."""
	return event == "SessionStart" and source in SESSION_SOURCES


# --- entry points ----------------------------------------------------------------


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
	return 0


def main(argv: list[str] | None = None) -> int:
	args = list(sys.argv[1:] if argv is None else argv)
	if args[:1] == ["--check"]:
		if len(args) > 2:
			print("usage: project_sync_prompt.py [--check [dir]]", file=sys.stderr)
			return 2
		target = args[1] if len(args) == 2 else os.getcwd()
		return check_config(cwd=target, home=os.path.expanduser("~"))
	if args:
		print("usage: project_sync_prompt.py [--check [dir]]", file=sys.stderr)
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
					"additionalContext": build_session_text(resolution.channels),
				}
			}
		)
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
