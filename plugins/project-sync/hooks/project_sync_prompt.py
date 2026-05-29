#!/usr/bin/env python3
"""Low-frequency task-management context for Claude Code SessionStart.

The hook runs on startup, resume, and explicit clear only. It never runs for
each user prompt or automatic compaction. Per-task before/after judgment lives
in the project-sync skill; this hook contributes one session-level reminder.

Channels are configured via TOML (see resolve_channels). Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping

DEFAULT_CHANNELS: list[dict] = [
	{"purpose": "task plans", "tool": "Linear", "when": ["before", "after"]},
	{"purpose": "decisions / notes", "tool": "Notion", "when": ["before", "after"]},
]

GLOBAL_CONFIG_RELPATH = ("project-sync", "config.toml")
PROJECT_CONFIG_NAME = ".project-sync.toml"
SESSION_SOURCES: set[str] = {"startup", "resume", "clear"}


# --- channel config ------------------------------------------------------------


def normalize_channel(raw: dict) -> dict | None:
	"""Coerce a raw config table into a channel, or None if it lacks purpose/tool."""
	purpose = str(raw.get("purpose", "")).strip()
	tool = str(raw.get("tool", "")).strip()
	if not purpose or not tool:
		return None
	when = raw.get("when")
	if not isinstance(when, list) or not when:
		when = ["before", "after"]
	return {"purpose": purpose, "tool": tool, "when": [str(w).strip() for w in when]}


def merge_channels(base: list[dict], override: list[dict]) -> list[dict]:
	"""Override base by purpose (case-insensitive); append unseen purposes."""
	merged = [dict(c) for c in base]
	index = {c["purpose"].lower(): i for i, c in enumerate(merged)}
	for c in override:
		key = c["purpose"].lower()
		if key in index:
			merged[index[key]] = dict(c)
		else:
			index[key] = len(merged)
			merged.append(dict(c))
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


def load_toml(path: str) -> dict | None:
	"""Parse a TOML file, returning None when it cannot be read."""
	try:
		with open(path, "rb") as f:
			return tomllib.load(f)
	except Exception:
		return None


def _channels_from(path: str | None) -> list[dict]:
	if not path:
		return []
	data = load_toml(path)
	if not isinstance(data, dict):
		return []
	out = []
	for raw in data.get("channel", []):
		if isinstance(raw, dict):
			c = normalize_channel(raw)
			if c:
				out.append(c)
	return out


def resolve_channels(
	cwd: str,
	home: str,
	env: Mapping[str, str] | None = None,
) -> list[dict]:
	"""Compose channels: built-in default <- global config <- per-project config."""
	environment: Mapping[str, str] = os.environ if env is None else env
	xdg = environment.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
	global_path = os.path.join(xdg, *GLOBAL_CONFIG_RELPATH)
	global_channels = _channels_from(global_path)
	project_channels = _channels_from(find_config_file(cwd, PROJECT_CONFIG_NAME))

	channels = (
		merge_channels(DEFAULT_CHANNELS, global_channels)
		if global_channels
		else [dict(c) for c in DEFAULT_CHANNELS]  # independent copy of the constant
	)
	if project_channels:
		channels = merge_channels(channels, project_channels)
	return channels


# --- session reminder ----------------------------------------------------------


def build_session_text(channels: list[dict]) -> str:
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
			f"Before finishing material work, use the project-sync skill to update "
			f"only changed project facts in the matching after channels: {targets}."
		)
	parts.append(
		"Infer the project from cwd or git remote, use the first configured "
		"available integration, and skip trivial or unrelated work."
	)
	return " ".join(parts)


def should_emit(event: str, source: str | None) -> bool:
	"""Return whether this invocation is an intended low-frequency session start."""
	return event == "SessionStart" and source in SESSION_SOURCES


def main() -> None:
	try:
		data = json.load(sys.stdin)
	except Exception:
		return
	if not isinstance(data, dict):
		return

	event = str(data.get("hook_event_name") or "")
	source_value = data.get("source")
	source = str(source_value) if source_value is not None else None
	if not should_emit(event, source):
		return

	cwd = data.get("cwd") or os.getcwd()
	channels = resolve_channels(cwd=cwd, home=os.path.expanduser("~"))

	print(
		json.dumps(
			{
				"hookSpecificOutput": {
					"hookEventName": "SessionStart",
					"additionalContext": build_session_text(channels),
				}
			}
		)
	)


if __name__ == "__main__":
	main()
