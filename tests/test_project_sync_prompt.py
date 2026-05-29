"""Tests for the low-frequency project-sync SessionStart hook."""

import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_DIR = os.path.join(
	REPO_ROOT,
	"plugins",
	"project-sync",
	"hooks",
)
sys.path.insert(0, HOOK_DIR)

import project_sync_prompt as h  # noqa: E402


# --- channel config ------------------------------------------------------------


class NormalizeChannel(unittest.TestCase):
	def test_defaults_when_to_before_and_after(self):
		c = h.normalize_channel({"purpose": "task plans", "tool": "Linear"})
		if c is None:
			self.fail("valid channel was rejected")
		self.assertEqual(c["when"], ["before", "after"])

	def test_preserves_explicit_when(self):
		c = h.normalize_channel(
			{"purpose": "logs", "tool": "Trackio", "when": ["after"]}
		)
		if c is None:
			self.fail("valid channel was rejected")
		self.assertEqual(c["when"], ["after"])

	def test_rejects_channel_missing_purpose_or_tool(self):
		self.assertIsNone(h.normalize_channel({"purpose": "x"}))
		self.assertIsNone(h.normalize_channel({"tool": "y"}))


class MergeChannels(unittest.TestCase):
	def test_override_replaces_same_purpose(self):
		base = [{"purpose": "task plans", "tool": "Linear", "when": ["before"]}]
		override = [{"purpose": "task plans", "tool": "Asana", "when": ["before"]}]
		merged = h.merge_channels(base, override)
		self.assertEqual(len(merged), 1)
		self.assertEqual(merged[0]["tool"], "Asana")

	def test_new_purpose_is_appended(self):
		base = [{"purpose": "task plans", "tool": "Linear", "when": ["before"]}]
		override = [
			{"purpose": "experiment results/logs", "tool": "Trackio", "when": ["after"]}
		]
		merged = h.merge_channels(base, override)
		self.assertEqual(len(merged), 2)
		self.assertEqual(merged[-1]["purpose"], "experiment results/logs")

	def test_purpose_match_is_case_insensitive(self):
		base = [{"purpose": "Task Plans", "tool": "Linear", "when": ["before"]}]
		override = [{"purpose": "task plans", "tool": "Asana", "when": ["before"]}]
		merged = h.merge_channels(base, override)
		self.assertEqual(len(merged), 1)
		self.assertEqual(merged[0]["tool"], "Asana")


class FindConfigFile(unittest.TestCase):
	def test_walks_up_to_find_file(self):
		with tempfile.TemporaryDirectory() as top:
			child = os.path.join(top, "a", "b", "c")
			os.makedirs(child)
			target = os.path.join(top, ".project-sync.toml")
			with open(target, "w") as f:
				f.write("")
			found = h.find_config_file(child, ".project-sync.toml")
			if found is None:
				self.fail("project config was not found")
			self.assertTrue(os.path.samefile(found, target))

	def test_returns_none_when_absent(self):
		with tempfile.TemporaryDirectory() as top:
			self.assertIsNone(h.find_config_file(top, ".project-sync.toml"))


class ResolveChannels(unittest.TestCase):
	def test_defaults_when_no_config_present(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			channels = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(channels, h.DEFAULT_CHANNELS)

	def test_returns_independent_copies_of_defaults(self):
		# Mutating a returned channel must not corrupt the module-level constant.
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			channels = h.resolve_channels(cwd=cwd, home=home, env={})
			channels[0]["tool"] = "MUTATED"
			self.assertNotEqual(h.DEFAULT_CHANNELS[0]["tool"], "MUTATED")

	def test_project_overrides_and_extends_global(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			gdir = os.path.join(home, ".config", "project-sync")
			os.makedirs(gdir)
			with open(os.path.join(gdir, "config.toml"), "w") as f:
				f.write(
					'[[channel]]\npurpose = "task plans"\ntool = "Linear"\n'
					'when = ["before", "after"]\n\n'
					'[[channel]]\npurpose = "decisions / notes"\ntool = "Notion"\n'
					'when = ["before", "after"]\n'
				)
			with open(os.path.join(cwd, ".project-sync.toml"), "w") as f:
				f.write(
					'[[channel]]\npurpose = "task plans"\ntool = "Asana"\n'
					'when = ["before", "after"]\n\n'
					'[[channel]]\npurpose = "experiment results/logs"\n'
					'tool = "Trackio"\nwhen = ["after"]\n'
				)
			channels = h.resolve_channels(cwd=cwd, home=home, env={})
			by_purpose = {c["purpose"].lower(): c for c in channels}
			self.assertEqual(by_purpose["task plans"]["tool"], "Asana")
			self.assertEqual(by_purpose["decisions / notes"]["tool"], "Notion")
			self.assertEqual(by_purpose["experiment results/logs"]["tool"], "Trackio")


# --- text builders -------------------------------------------------------------


class BuildSessionText(unittest.TestCase):
	def test_lists_before_and_after_channels(self):
		text = h.build_session_text(h.DEFAULT_CHANNELS)
		self.assertIn("before acting", text.lower())
		self.assertIn("Before finishing", text)
		self.assertIn("Linear", text)
		self.assertIn("Notion", text)

	def test_respects_channel_phases(self):
		channels = [
			{"purpose": "task plans", "tool": "Linear", "when": ["before"]},
			{"purpose": "experiment logs", "tool": "Trackio", "when": ["after"]},
		]
		text = h.build_session_text(channels)
		before_text, after_text = text.split("Before finishing", maxsplit=1)
		self.assertIn("Linear", before_text)
		self.assertNotIn("Trackio", before_text)
		self.assertIn("experiment logs", after_text)
		self.assertIn("Trackio", after_text)
		self.assertNotIn("Linear", after_text)


class ShouldEmit(unittest.TestCase):
	def test_allows_low_frequency_session_sources(self):
		for source in ("startup", "resume", "clear"):
			with self.subTest(source=source):
				self.assertTrue(h.should_emit("SessionStart", source))

	def test_rejects_compact_and_per_prompt_events(self):
		self.assertFalse(h.should_emit("SessionStart", "compact"))
		self.assertFalse(h.should_emit("UserPromptSubmit", None))

	def test_rejects_missing_event_or_source(self):
		self.assertFalse(h.should_emit("SessionStart", None))
		self.assertFalse(h.should_emit("", "startup"))


class HookManifest(unittest.TestCase):
	def test_registers_only_filtered_session_start(self):
		path = os.path.join(HOOK_DIR, "hooks.json")
		with open(path, encoding="utf-8") as f:
			hooks = json.load(f)["hooks"]
		self.assertEqual(set(hooks), {"SessionStart"})
		self.assertEqual(hooks["SessionStart"][0]["matcher"], "startup|resume|clear")


if __name__ == "__main__":
	unittest.main()
