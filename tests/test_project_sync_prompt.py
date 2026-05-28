"""Tests for the configurable project-sync hook logic. Stdlib unittest only."""

import os
import sys
import tempfile
import unittest

HOOK_DIR = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
	"plugins",
	"project-sync",
	"hooks",
)
sys.path.insert(0, HOOK_DIR)

import project_sync_prompt as h  # noqa: E402


class NormalizeChannel(unittest.TestCase):
	def test_defaults_when_to_before_and_after(self):
		c = h.normalize_channel({"purpose": "task plans", "tool": "Linear"})
		self.assertEqual(c["when"], ["before", "after"])

	def test_preserves_explicit_when(self):
		c = h.normalize_channel(
			{"purpose": "logs", "tool": "Trackio", "when": ["after"]}
		)
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


class BuildReminder(unittest.TestCase):
	def test_default_no_state_lists_before_channels(self):
		text = h.build_reminder(h.DEFAULT_CHANNELS, [])
		self.assertIn("before acting", text)
		self.assertIn("Linear", text)
		self.assertIn("Notion", text)
		self.assertNotIn("⚠", text)  # no warning sign without state change

	def test_state_change_adds_sync_back_augment(self):
		text = h.build_reminder(h.DEFAULT_CHANNELS, ['"Edit"', "git commit"])
		self.assertIn("⚠", text)
		self.assertIn("git commit", text)
		self.assertIn("Linear", text)

	def test_respects_when_phase(self):
		channels = [
			{"purpose": "task plans", "tool": "Linear", "when": ["before"]},
			{"purpose": "experiment logs", "tool": "Trackio", "when": ["after"]},
		]
		base = h.build_reminder(channels, [])
		self.assertIn("Linear", base)
		self.assertNotIn("Trackio", base)  # after-only stays out of the before nudge
		augmented = h.build_reminder(channels, ['"Write"'])
		self.assertIn("Trackio", augmented)


class DetectState(unittest.TestCase):
	def test_returns_sorted_unique_hits(self):
		tail = 'noise "Edit" noise git commit noise "Edit"'
		hits = h.detect_state(tail, h.STATE_SIGNALS)
		self.assertIn('"Edit"', hits)
		self.assertIn("git commit", hits)
		self.assertEqual(hits, sorted(set(hits)))


class FindConfigFile(unittest.TestCase):
	def test_walks_up_to_find_file(self):
		with tempfile.TemporaryDirectory() as top:
			child = os.path.join(top, "a", "b", "c")
			os.makedirs(child)
			target = os.path.join(top, ".project-sync.toml")
			with open(target, "w") as f:
				f.write("")
			found = h.find_config_file(child, ".project-sync.toml")
			self.assertIsNotNone(found)
			self.assertTrue(os.path.samefile(found, target))

	def test_returns_none_when_absent(self):
		with tempfile.TemporaryDirectory() as top:
			self.assertIsNone(h.find_config_file(top, ".project-sync.toml"))


class ResolveChannels(unittest.TestCase):
	def test_defaults_when_no_config_present(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			channels = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(channels, h.DEFAULT_CHANNELS)

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


if __name__ == "__main__":
	unittest.main()
