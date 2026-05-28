#!/usr/bin/env python3
"""UserPromptSubmit hook: inject a configurable task-management-first reminder.

Tool- and project-agnostic. Reminds the agent to consult its configured "sync
channels" (e.g. a task tracker like Linear, project notes like Notion, an
experiment tracker like Trackio) before acting, and to write state changes back
afterward. If the previous turn made state changes, the reminder is augmented
with a "sync back" nudge.

Channels are configured via TOML (see resolve_channels). With no config present,
a sensible default is used. Reads the hook JSON from stdin and emits a
UserPromptSubmit additionalContext block. Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

DEFAULT_CHANNELS: list[dict] = [
	{"purpose": "task plans", "tool": "Linear", "when": ["before", "after"]},
	{"purpose": "decisions / notes", "tool": "Notion", "when": ["before", "after"]},
]

# Substrings (NOT full tool prefixes) so detection works across MCP namings.
STATE_SIGNALS: list[str] = [
	'"Edit"',
	'"Write"',
	'"MultiEdit"',
	'"NotebookEdit"',
	"save_issue",
	"notion-update",
	"notion-create",
	"git commit",
	"git push",
	"gh pr create",
	"gh issue create",
]

GLOBAL_CONFIG_RELPATH = ("project-sync", "config.toml")
PROJECT_CONFIG_NAME = ".project-sync.toml"


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


def build_reminder(channels: list[dict], state_hits: list[str]) -> str:
	"""Render the additionalContext string from channels + detected state changes."""
	before = [c for c in channels if "before" in c["when"]]
	after = [c for c in channels if "after" in c["when"]]

	if before:
		checks = "; ".join(
			f"check {c['purpose']} in your {c['tool']} MCP (or equivalent)"
			for c in before
		)
		base = (
			f"Task-management reminder: before acting, {checks}. "
			"Infer the project from cwd / git remote. Adapt to whatever MCP tools "
			"exist on this machine — do not assume specific tool names. Skip if the "
			"task is trivial or unrelated to any tracked project."
		)
	else:
		base = (
			"Task-management reminder: work task-management-first; consult your "
			"project's tracker and notes before acting. Skip if trivial or unrelated."
		)

	if not state_hits:
		return base

	targets = "; ".join(f"update {c['purpose']} in {c['tool']}" for c in after)
	augment = (
		f"\n\n⚠ Previous turn made state changes ({', '.join(state_hits[:5])}) — "
		"before starting new work, reflect those changes back"
	)
	augment += f": {targets}." if targets else " into the relevant tracker/notes."
	return base + augment


def detect_state(tail: str, signals: list[str]) -> list[str]:
	"""Return the sorted, de-duplicated state signals present in the transcript tail."""
	return sorted({s for s in signals if s in tail})


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
	"""Parse a TOML file. Returns None if no parser is available or parsing fails."""
	try:
		import tomllib  # Python 3.11+
	except ModuleNotFoundError:
		try:
			import tomli as tomllib  # type: ignore[no-redef]
		except ModuleNotFoundError:
			return None
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


def resolve_channels(cwd: str, home: str, env: dict | None = None) -> list[dict]:
	"""Compose channels: built-in default <- global config <- per-project config."""
	env = os.environ if env is None else env
	xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
	global_path = os.path.join(xdg, *GLOBAL_CONFIG_RELPATH)
	global_channels = _channels_from(
		global_path if os.path.isfile(global_path) else None
	)
	project_channels = _channels_from(find_config_file(cwd, PROJECT_CONFIG_NAME))

	channels = (
		merge_channels(DEFAULT_CHANNELS, global_channels)
		if global_channels
		else list(DEFAULT_CHANNELS)
	)
	if project_channels:
		channels = merge_channels(channels, project_channels)
	return channels


def main() -> None:
	try:
		data = json.load(sys.stdin)
	except Exception:
		data = {}

	cwd = data.get("cwd") or os.getcwd()
	channels = resolve_channels(cwd=cwd, home=os.path.expanduser("~"))

	state_hits: list[str] = []
	tpath = data.get("transcript_path", "")
	if tpath and os.path.exists(tpath):
		try:
			tail = subprocess.check_output(
				["tail", "-n", "400", tpath],
				text=True,
				stderr=subprocess.DEVNULL,
				timeout=2,
			)
			state_hits = detect_state(tail, STATE_SIGNALS)
		except Exception:
			state_hits = []

	print(
		json.dumps(
			{
				"hookSpecificOutput": {
					"hookEventName": "UserPromptSubmit",
					"additionalContext": build_reminder(channels, state_hits),
				}
			}
		)
	)


if __name__ == "__main__":
	main()
