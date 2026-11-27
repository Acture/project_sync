"""Tests for the low-frequency project-sync SessionStart hook."""

import json
import os
import subprocess
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
HOOK_PATH = os.path.join(HOOK_DIR, "project_sync_prompt.py")
sys.path.insert(0, HOOK_DIR)

import project_sync_prompt as h  # noqa: E402


def write(path: str, text: str) -> None:
	with open(path, "w", encoding="utf-8") as f:
		f.write(text)


# --- channel config ------------------------------------------------------------


class NormalizeChannel(unittest.TestCase):
	def test_defaults_when_to_before_and_after(self):
		c = h.normalize_channel({"purpose": "task plans", "tool": "Linear"})
		self.assertEqual(c["when"], ["before", "after"])
		self.assertIs(c["enabled"], True)

	def test_preserves_explicit_when(self):
		c = h.normalize_channel(
			{"purpose": "logs", "tool": "Trackio", "when": ["after"]}
		)
		self.assertEqual(c["when"], ["after"])

	def test_dedupes_repeated_phases(self):
		c = h.normalize_channel(
			{"purpose": "logs", "tool": "Trackio", "when": ["after", "before", "after"]}
		)
		self.assertEqual(c["when"], ["after", "before"])

	def test_rejects_missing_purpose(self):
		with self.assertRaises(h.ConfigError):
			h.normalize_channel({"tool": "y"})

	def test_rejects_empty_or_non_string_purpose(self):
		for raw in ({"purpose": "  ", "tool": "y"}, {"purpose": 3, "tool": "y"}):
			with self.subTest(raw=raw), self.assertRaises(h.ConfigError):
				h.normalize_channel(raw)

	def test_rejects_missing_or_empty_tool(self):
		for raw in ({"purpose": "x"}, {"purpose": "x", "tool": ""}):
			with self.subTest(raw=raw), self.assertRaises(h.ConfigError):
				h.normalize_channel(raw)

	def test_rejects_non_boolean_enabled(self):
		with self.assertRaises(h.ConfigError):
			h.normalize_channel({"purpose": "x", "tool": "y", "enabled": "no"})

	def test_empty_when_is_rejected_not_defaulted(self):
		with self.assertRaises(h.ConfigError):
			h.normalize_channel({"purpose": "x", "tool": "y", "when": []})

	def test_when_must_be_a_list(self):
		for when in ("before", 3, True):
			with self.subTest(when=when), self.assertRaises(h.ConfigError):
				h.normalize_channel({"purpose": "x", "tool": "y", "when": when})

	def test_when_rejects_unknown_phase_names(self):
		for when in (["before", "later"], [1]):
			with self.subTest(when=when), self.assertRaises(h.ConfigError):
				h.normalize_channel({"purpose": "x", "tool": "y", "when": when})

	def test_disable_directive_needs_only_purpose(self):
		c = h.normalize_channel({"purpose": "decisions / notes", "enabled": False})
		self.assertIs(c["enabled"], False)
		self.assertEqual(c["purpose"], "decisions / notes")


class MergeChannels(unittest.TestCase):
	def test_override_replaces_same_purpose(self):
		base: list[h.Channel] = [
			{"purpose": "task plans", "tool": "Linear", "when": ["before"]}
		]
		override: list[h.Channel] = [
			{"purpose": "task plans", "tool": "Asana", "when": ["before"]}
		]
		merged = h.merge_channels(base, override)
		self.assertEqual(len(merged), 1)
		self.assertEqual(merged[0]["tool"], "Asana")

	def test_new_purpose_is_appended(self):
		base: list[h.Channel] = [
			{"purpose": "task plans", "tool": "Linear", "when": ["before"]}
		]
		override: list[h.Channel] = [
			{"purpose": "experiment results/logs", "tool": "Trackio", "when": ["after"]}
		]
		merged = h.merge_channels(base, override)
		self.assertEqual(len(merged), 2)
		self.assertEqual(merged[-1]["purpose"], "experiment results/logs")

	def test_purpose_match_is_case_insensitive(self):
		base: list[h.Channel] = [
			{"purpose": "Task Plans", "tool": "Linear", "when": ["before"]}
		]
		override: list[h.Channel] = [
			{"purpose": "task plans", "tool": "Asana", "when": ["before"]}
		]
		merged = h.merge_channels(base, override)
		self.assertEqual(len(merged), 1)
		self.assertEqual(merged[0]["tool"], "Asana")

	def test_disable_directive_overrides_existing_channel(self):
		base: list[h.Channel] = [
			{"purpose": "task plans", "tool": "Linear", "when": ["before"]}
		]
		directive = h.normalize_channel({"purpose": "task plans", "enabled": False})
		merged = h.merge_channels(base, [directive])
		self.assertEqual(len(merged), 1)
		self.assertIs(merged[0].get("enabled"), False)


class FindConfigFile(unittest.TestCase):
	def test_walks_up_to_find_file(self):
		with tempfile.TemporaryDirectory() as top:
			child = os.path.join(top, "a", "b", "c")
			os.makedirs(child)
			target = os.path.join(top, ".project-sync.toml")
			write(target, "")
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
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(resolution.errors, [])
			self.assertEqual(resolution.channels, h.DEFAULT_CHANNELS)

	def test_returns_independent_copies_of_defaults(self):
		# Mutating a returned channel must not corrupt the module-level constant.
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			resolution.channels[0]["tool"] = "MUTATED"
			resolution.channels[0]["when"].append("before")
			self.assertNotEqual(h.DEFAULT_CHANNELS[0]["tool"], "MUTATED")
			self.assertEqual(h.DEFAULT_CHANNELS[0]["when"], ["before", "after"])

	def test_project_overrides_and_extends_global(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			gdir = os.path.join(home, ".config", "project-sync")
			os.makedirs(gdir)
			write(
				os.path.join(gdir, "config.toml"),
				'[[channel]]\npurpose = "task plans"\ntool = "Linear"\n'
				'when = ["before", "after"]\n\n'
				'[[channel]]\npurpose = "decisions / notes"\ntool = "Notion"\n'
				'when = ["before", "after"]\n',
			)
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\ntool = "Asana"\n'
				'when = ["before", "after"]\n\n'
				'[[channel]]\npurpose = "experiment results/logs"\n'
				'tool = "Trackio"\nwhen = ["after"]\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(resolution.errors, [])
			by_purpose = {c["purpose"].lower(): c for c in resolution.channels}
			self.assertEqual(by_purpose["task plans"]["tool"], "Asana")
			self.assertEqual(by_purpose["decisions / notes"]["tool"], "Notion")
			self.assertEqual(
				by_purpose["decisions / notes"]["when"], ["before", "after"]
			)
			self.assertEqual(by_purpose["experiment results/logs"]["tool"], "Trackio")

	def test_project_disables_default_channel(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "decisions / notes"\nenabled = false\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(resolution.errors, [])
			self.assertEqual(
				[c["purpose"] for c in resolution.channels],
				["task plans"],
			)

	def test_project_reenables_globally_disabled_channel(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			gdir = os.path.join(home, ".config", "project-sync")
			os.makedirs(gdir)
			write(
				os.path.join(gdir, "config.toml"),
				'[[channel]]\npurpose = "decisions / notes"\nenabled = false\n',
			)
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "decisions / notes"\ntool = "Obsidian"\n'
				'when = ["after"]\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(resolution.errors, [])
			by_purpose = {c["purpose"].lower(): c for c in resolution.channels}
			self.assertEqual(by_purpose["decisions / notes"]["tool"], "Obsidian")
			self.assertEqual(by_purpose["decisions / notes"]["when"], ["after"])

	def test_disabling_unknown_purpose_is_a_no_op(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "no such channel"\nenabled = false\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(resolution.errors, [])
			self.assertEqual(resolution.channels, h.DEFAULT_CHANNELS)

	def assert_fails_closed(self, resolution: h.Resolution, needle: str) -> None:
		self.assertEqual(resolution.channels, [])
		self.assertTrue(resolution.errors)
		self.assertIn(needle, resolution.errors[0])

	def test_damaged_project_toml_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(os.path.join(cwd, ".project-sync.toml"), "[[channel]\n")
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, ".project-sync.toml")

	def test_damaged_global_toml_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			gdir = os.path.join(home, ".config", "project-sync")
			os.makedirs(gdir)
			write(os.path.join(gdir, "config.toml"), "[[channel]\n")
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "config.toml")

	def test_non_array_channel_key_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(os.path.join(cwd, ".project-sync.toml"), "channel = 1\n")
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "[[channel]]")

	def test_single_channel_table_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[channel]\npurpose = "x"\ntool = "y"\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "[[channel]]")

	def test_empty_when_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\ntool = "y"\nwhen = []\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "when")

	def test_non_list_when_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\ntool = "y"\nwhen = "before"\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "when")

	def test_unknown_phase_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\ntool = "y"\nwhen = ["before", "later"]\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "when")

	def test_invalid_channel_entry_fails_closed(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "channel[0]")

	def test_unreadable_config_fails_closed(self):
		if hasattr(os, "geteuid") and os.geteuid() == 0:
			self.skipTest("permission checks are bypassed as root")
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			gdir = os.path.join(home, ".config", "project-sync")
			os.makedirs(gdir)
			path = os.path.join(gdir, "config.toml")
			write(path, '[[channel]]\npurpose = "x"\ntool = "y"\n')
			os.chmod(path, 0o000)
			try:
				resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			finally:
				os.chmod(path, 0o600)
			self.assert_fails_closed(resolution, "permission")


# --- text builders -------------------------------------------------------------


class BuildSessionText(unittest.TestCase):
	def test_lists_before_and_after_channels(self):
		text = h.build_session_text(h.DEFAULT_CHANNELS)
		self.assertIn("before acting", text.lower())
		self.assertIn("Before finishing", text)
		self.assertIn("Linear", text)
		self.assertIn("Notion", text)

	def test_respects_channel_phases(self):
		channels: list[h.Channel] = [
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

	def test_phases_never_grant_write_access(self):
		channels: list[h.Channel] = [
			{"purpose": "task plans", "tool": "Linear", "when": ["before", "after"]},
		]
		text = h.build_session_text(channels)
		self.assertIn("never grant write access", text)

	def test_empty_channel_list_omits_phase_instructions(self):
		text = h.build_session_text([])
		self.assertNotIn("Before acting", text)
		self.assertNotIn("Before finishing", text)


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


# --- hook process boundary -----------------------------------------------------


class HookBoundary(unittest.TestCase):
	def run_hook(
		self,
		*args: str,
		stdin_text: str = "",
		cwd: str,
		home: str,
	) -> subprocess.CompletedProcess[str]:
		env = dict(os.environ)
		env["HOME"] = home
		env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
		return subprocess.run(
			[sys.executable, HOOK_PATH, *args],
			input=stdin_text,
			capture_output=True,
			text=True,
			cwd=cwd,
			env=env,
			timeout=30,
		)

	def session_input(self, cwd: str, source: str = "startup") -> str:
		return json.dumps(
			{"hook_event_name": "SessionStart", "source": source, "cwd": cwd}
		)

	def test_startup_injects_session_context(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			result = self.run_hook(
				stdin_text=self.session_input(cwd), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stderr, "")
			payload = json.loads(result.stdout)
			context = payload["hookSpecificOutput"]["additionalContext"]
			self.assertIn("Project-sync session reminder", context)

	def test_compact_source_stays_silent(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			result = self.run_hook(
				stdin_text=self.session_input(cwd, source="compact"), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")

	def test_damaged_config_injects_nothing(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(os.path.join(cwd, ".project-sync.toml"), "[[channel]\n")
			result = self.run_hook(
				stdin_text=self.session_input(cwd), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")
			self.assertIn("project-sync:", result.stderr)
			self.assertIn(".project-sync.toml", result.stderr)

	def test_all_channels_disabled_injects_nothing(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\nenabled = false\n\n'
				'[[channel]]\npurpose = "decisions / notes"\nenabled = false\n',
			)
			result = self.run_hook(
				stdin_text=self.session_input(cwd), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")
			self.assertEqual(result.stderr, "")

	def test_check_mode_fails_for_damaged_config(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(os.path.join(cwd, ".project-sync.toml"), "[[channel]\n")
			result = self.run_hook("--check", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 1)
			self.assertEqual(result.stdout, "")
			self.assertIn("project-sync:", result.stderr)

	def test_check_mode_ok_without_config(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			result = self.run_hook("--check", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stderr, "")
			self.assertIn("task plans -> Linear", result.stdout)

	def test_check_mode_reports_all_disabled(self):
		with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\nenabled = false\n\n'
				'[[channel]]\npurpose = "decisions / notes"\nenabled = false\n',
			)
			result = self.run_hook("--check", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 0)
			self.assertIn("disabled", result.stdout)


if __name__ == "__main__":
	unittest.main()
