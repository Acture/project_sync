"""Tests for the low-frequency project-sync SessionStart hook."""

import json
import os
import shutil
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


PARENT_CONFIG = (
	'[[channel]]\npurpose = "task plans"\ntool = "ParentTool"\n'
	'when = ["before", "after"]\n\n'
	'[[channel]]\npurpose = "experiment results/logs"\ntool = "Trackio"\n'
	'when = ["after"]\n'
)


class FindProjectConfigs(unittest.TestCase):
	def test_walks_up_to_find_file(self):
		with tempfile.TemporaryDirectory() as top:
			child = os.path.join(top, "a", "b", "c")
			os.makedirs(child)
			target = os.path.join(top, ".project-sync.toml")
			write(target, "")
			found, boundary = h.find_project_configs(child)
			self.assertIsNone(boundary)
			self.assertEqual(len(found), 1)
			self.assertTrue(os.path.samefile(found[0], target))

	def test_returns_empty_when_absent(self):
		with tempfile.TemporaryDirectory() as top:
			found, boundary = h.find_project_configs(top)
			self.assertEqual(found, [])
			self.assertIsNone(boundary)


class ResolveChannels(unittest.TestCase):
	def test_defaults_when_no_config_present(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(resolution.errors, [])
			self.assertEqual(resolution.channels, h.DEFAULT_CHANNELS)

	def test_returns_independent_copies_of_defaults(self):
		# Mutating a returned channel must not corrupt the module-level constant.
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			resolution.channels[0]["tool"] = "MUTATED"
			resolution.channels[0]["when"].append("before")
			self.assertNotEqual(h.DEFAULT_CHANNELS[0]["tool"], "MUTATED")
			self.assertEqual(h.DEFAULT_CHANNELS[0]["when"], ["before", "after"])

	def test_project_overrides_and_extends_global(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
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
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
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
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
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
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
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
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(os.path.join(cwd, ".project-sync.toml"), "[[channel]\n")
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, ".project-sync.toml")

	def test_damaged_global_toml_fails_closed(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			gdir = os.path.join(home, ".config", "project-sync")
			os.makedirs(gdir)
			write(os.path.join(gdir, "config.toml"), "[[channel]\n")
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "config.toml")

	def test_non_array_channel_key_fails_closed(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(os.path.join(cwd, ".project-sync.toml"), "channel = 1\n")
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "[[channel]]")

	def test_single_channel_table_fails_closed(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[channel]\npurpose = "x"\ntool = "y"\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "[[channel]]")

	def test_empty_when_fails_closed(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\ntool = "y"\nwhen = []\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "when")

	def test_non_list_when_fails_closed(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\ntool = "y"\nwhen = "before"\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "when")

	def test_unknown_phase_fails_closed(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\ntool = "y"\nwhen = ["before", "later"]\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "when")

	def test_invalid_channel_entry_fails_closed(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "x"\n',
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assert_fails_closed(resolution, "channel[0]")

	def test_unreadable_config_fails_closed(self):
		if hasattr(os, "geteuid") and os.geteuid() == 0:
			self.skipTest("permission checks are bypassed as root")
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
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


# --- git / linked-worktree boundary --------------------------------------------


class WorktreeBoundary(unittest.TestCase):
	def by_purpose(self, resolution: h.Resolution) -> dict[str, h.Channel]:
		return {c["purpose"].lower(): c for c in resolution.channels}

	def test_clone_stops_at_worktree_root(self):
		with (
			tempfile.TemporaryDirectory() as top,
			tempfile.TemporaryDirectory() as home,
		):
			write(os.path.join(top, ".project-sync.toml"), PARENT_CONFIG)
			repo = os.path.join(top, "repo")
			os.makedirs(os.path.join(repo, ".git"))
			write(
				os.path.join(repo, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\ntool = "RepoTool"\n',
			)
			sub = os.path.join(repo, "a", "b")
			os.makedirs(sub)
			resolution = h.resolve_channels(cwd=sub, home=home, env={})
			self.assertEqual(resolution.errors, [])
			by_purpose = self.by_purpose(resolution)
			self.assertEqual(by_purpose["task plans"]["tool"], "RepoTool")
			self.assertNotIn("experiment results/logs", by_purpose)
			self.assertEqual(resolution.worktree_root, os.path.realpath(repo))

	def test_linked_worktree_resolves_like_a_clone(self):
		# A linked worktree has a .git *file*, not a directory; the boundary
		# must treat both shapes identically.
		with (
			tempfile.TemporaryDirectory() as top,
			tempfile.TemporaryDirectory() as home,
		):
			write(os.path.join(top, ".project-sync.toml"), PARENT_CONFIG)
			wt = os.path.join(top, "wt")
			os.makedirs(wt)
			write(os.path.join(wt, ".git"), "gitdir: /nonexistent/gitdir\n")
			write(
				os.path.join(wt, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\ntool = "WtTool"\n',
			)
			sub = os.path.join(wt, "x")
			os.makedirs(sub)
			resolution = h.resolve_channels(cwd=sub, home=home, env={})
			self.assertEqual(resolution.errors, [])
			by_purpose = self.by_purpose(resolution)
			self.assertEqual(by_purpose["task plans"]["tool"], "WtTool")
			self.assertNotIn("experiment results/logs", by_purpose)
			self.assertEqual(resolution.worktree_root, os.path.realpath(wt))

	def test_parent_inheritance_requires_explicit_opt_in(self):
		with (
			tempfile.TemporaryDirectory() as top,
			tempfile.TemporaryDirectory() as home,
		):
			write(
				os.path.join(top, ".project-sync.toml"),
				PARENT_CONFIG + '\n[project]\nname = "parent-name"\n',
			)
			repo = os.path.join(top, "repo")
			os.makedirs(os.path.join(repo, ".git"))
			write(
				os.path.join(repo, ".project-sync.toml"),
				'[project]\ninherit_parent = true\nlinear = "OSS"\n\n'
				'[[channel]]\npurpose = "task plans"\ntool = "RepoTool"\n',
			)
			sub = os.path.join(repo, "sub")
			os.makedirs(sub)
			resolution = h.resolve_channels(cwd=sub, home=home, env={})
			self.assertEqual(resolution.errors, [])
			by_purpose = self.by_purpose(resolution)
			# The repo layer wins over the inherited parent layer.
			self.assertEqual(by_purpose["task plans"]["tool"], "RepoTool")
			# A parent-only purpose is appended.
			self.assertEqual(by_purpose["experiment results/logs"]["tool"], "Trackio")
			# Project bindings merge across layers; nearest file wins per field.
			self.assertEqual(resolution.project.name, "parent-name")
			self.assertEqual(resolution.project.linear, "OSS")
			self.assertTrue(resolution.project.inherit_parent)
			self.assertEqual(
				[
					os.path.basename(os.path.dirname(p))
					for p in resolution.project_paths
				],
				["repo", os.path.basename(top)],
			)

	@unittest.skipUnless(shutil.which("git"), "git not available")
	def test_real_linked_worktree_matches_clone_boundary(self):
		with (
			tempfile.TemporaryDirectory() as top,
			tempfile.TemporaryDirectory() as home,
		):
			repo = os.path.join(top, "repo")
			os.makedirs(repo)

			def git(*args: str) -> None:
				subprocess.run(
					["git", *args],
					cwd=repo,
					check=True,
					capture_output=True,
					text=True,
					timeout=30,
				)

			git("init", "-q")
			write(os.path.join(repo, "tracked.txt"), "x\n")
			git("add", "tracked.txt")
			git(
				"-c",
				"user.name=t",
				"-c",
				"user.email=t@example.com",
				"commit",
				"-qm",
				"init",
			)
			write(os.path.join(top, ".project-sync.toml"), PARENT_CONFIG)
			write(
				os.path.join(repo, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\ntool = "RepoTool"\n',
			)
			wt = os.path.join(top, "wt")
			git("worktree", "add", "-q", wt)
			write(
				os.path.join(wt, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\ntool = "WtTool"\n',
			)

			clone_res = h.resolve_channels(cwd=repo, home=home, env={})
			wt_res = h.resolve_channels(cwd=wt, home=home, env={})
			self.assertEqual(clone_res.errors, [])
			self.assertEqual(wt_res.errors, [])
			self.assertEqual(
				self.by_purpose(clone_res)["task plans"]["tool"], "RepoTool"
			)
			self.assertEqual(self.by_purpose(wt_res)["task plans"]["tool"], "WtTool")
			self.assertNotIn("experiment results/logs", self.by_purpose(clone_res))
			self.assertNotIn("experiment results/logs", self.by_purpose(wt_res))
			self.assertEqual(clone_res.worktree_root, os.path.realpath(repo))
			self.assertEqual(wt_res.worktree_root, os.path.realpath(wt))


# --- explicit project binding ----------------------------------------------------


class ProjectBindingConfig(unittest.TestCase):
	def test_explicit_binding_is_reported(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			path = os.path.join(cwd, ".project-sync.toml")
			write(path, '[project]\nname = "oss-sync"\nlinear = "OSS"\n')
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			self.assertEqual(resolution.errors, [])
			self.assertEqual(resolution.project.name, "oss-sync")
			self.assertEqual(resolution.project.linear, "OSS")
			self.assertEqual(resolution.project.source, os.path.realpath(path))

	def test_invalid_project_table_fails_closed(self):
		for body in (
			'[project]\nname = ""\n',
			'[project]\ninherit_parent = "yes"\n',
			"project = 1\n",
		):
			with (
				self.subTest(body=body),
				tempfile.TemporaryDirectory() as home,
				tempfile.TemporaryDirectory() as cwd,
			):
				write(os.path.join(cwd, ".project-sync.toml"), body)
				resolution = h.resolve_channels(cwd=cwd, home=home, env={})
				self.assertEqual(resolution.channels, [])
				self.assertTrue(resolution.errors)

	def test_binding_appears_in_session_text(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"), '[project]\nlinear = "OSS"\n'
			)
			resolution = h.resolve_channels(cwd=cwd, home=home, env={})
			text = h.build_session_text(resolution.channels, resolution.project)
			self.assertIn("OSS", text)
			self.assertIn("instead of searching by repository name", text)
			self.assertNotIn("Infer the project from cwd", text)


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
		for source in ("startup", "resume", "clear", "fork"):
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
		self.assertEqual(
			hooks["SessionStart"][0]["matcher"], "startup|resume|clear|fork"
		)


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
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			result = self.run_hook(
				stdin_text=self.session_input(cwd), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stderr, "")
			payload = json.loads(result.stdout)
			context = payload["hookSpecificOutput"]["additionalContext"]
			self.assertIn("Project-sync session reminder", context)

	def test_compact_source_stays_silent(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			result = self.run_hook(
				stdin_text=self.session_input(cwd, source="compact"), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")

	def test_damaged_config_injects_nothing(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(os.path.join(cwd, ".project-sync.toml"), "[[channel]\n")
			result = self.run_hook(
				stdin_text=self.session_input(cwd), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")
			self.assertIn("project-sync:", result.stderr)
			self.assertIn(".project-sync.toml", result.stderr)

	def test_all_channels_disabled_injects_nothing(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
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
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(os.path.join(cwd, ".project-sync.toml"), "[[channel]\n")
			result = self.run_hook("--check", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 1)
			self.assertEqual(result.stdout, "")
			self.assertIn("project-sync:", result.stderr)

	def test_check_mode_ok_without_config(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			result = self.run_hook("--check", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stderr, "")
			self.assertIn("task plans -> Linear", result.stdout)

	def test_check_mode_reports_all_disabled(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\nenabled = false\n\n'
				'[[channel]]\npurpose = "decisions / notes"\nenabled = false\n',
			)
			result = self.run_hook("--check", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 0)
			self.assertIn("disabled", result.stdout)

	def effective(self, cwd: str, home: str) -> tuple[int, dict[str, object], str]:
		result = self.run_hook("--effective", cwd=cwd, home=home)
		return result.returncode, json.loads(result.stdout), result.stderr

	def hook_context(self, cwd: str, home: str, source: str = "startup") -> str:
		result = self.run_hook(
			stdin_text=self.session_input(cwd, source=source), cwd=cwd, home=home
		)
		self.assertEqual(result.returncode, 0)
		payload = json.loads(result.stdout)
		context = payload["hookSpecificOutput"]["additionalContext"]
		if not isinstance(context, str):
			self.fail("additionalContext is not a string")
		return context

	def test_fork_source_injects_session_context(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			context = self.hook_context(cwd, home, source="fork")
			self.assertIn("Project-sync session reminder", context)

	def test_effective_defaults_without_config(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			rc, payload, stderr = self.effective(cwd, home)
			self.assertEqual(rc, 0)
			self.assertEqual(stderr, "")
			self.assertIs(payload["ok"], True)
			channels = payload["channels"]
			if not isinstance(channels, list):
				self.fail("channels is not a list")
			self.assertEqual(
				[c["purpose"] for c in channels],
				["task plans", "decisions / notes"],
			)
			sources = payload["sources"]
			if not isinstance(sources, dict):
				self.fail("sources is not an object")
			global_source = sources["global"]
			if not isinstance(global_source, dict):
				self.fail("global source is not an object")
			self.assertIs(global_source["exists"], False)
			self.assertEqual(sources["project"], [])

	def test_effective_matches_hook_text_under_partial_config(self):
		# A config that overrides one default and adds one channel: the hook
		# reminder and the --effective JSON must describe the same channels.
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "task plans"\ntool = "Asana"\n'
				'when = ["before"]\n\n'
				'[[channel]]\npurpose = "experiment results/logs"\ntool = "Trackio"\n'
				'when = ["after"]\n',
			)
			context = self.hook_context(cwd, home)
			rc, payload, _ = self.effective(cwd, home)
			self.assertEqual(rc, 0)
			before_text, after_text = context.split("Before finishing", maxsplit=1)
			channels = payload["channels"]
			if not isinstance(channels, list):
				self.fail("channels is not a list")
			for channel in channels:
				segment = f"{channel['purpose']} in {channel['tool']}"
				if "before" in channel["when"]:
					self.assertIn(segment, before_text)
				if "after" in channel["when"]:
					self.assertIn(segment, after_text)
			# The untouched default stays before-only in both views.
			self.assertIn("decisions / notes in Notion", before_text)
			self.assertNotIn("Notion", after_text)

	def test_disabled_destination_not_reauthorized_by_text(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(
				os.path.join(cwd, ".project-sync.toml"),
				'[[channel]]\npurpose = "decisions / notes"\nenabled = false\n',
			)
			context = self.hook_context(cwd, home)
			self.assertNotIn("Notion", context)
			self.assertIn("task plans", context)
			self.assertIn("never grant write access", context)
			rc, payload, _ = self.effective(cwd, home)
			self.assertEqual(rc, 0)
			channels = payload["channels"]
			if not isinstance(channels, list):
				self.fail("channels is not a list")
			self.assertEqual([c["purpose"] for c in channels], ["task plans"])
			self.assertEqual(payload["disabled"], ["decisions / notes"])

	def test_effective_fails_closed_for_damaged_config(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			write(os.path.join(cwd, ".project-sync.toml"), "[[channel]\n")
			rc, payload, stderr = self.effective(cwd, home)
			self.assertEqual(rc, 1)
			self.assertIs(payload["ok"], False)
			self.assertEqual(payload["channels"], [])
			errors = payload["errors"]
			if not isinstance(errors, list):
				self.fail("errors is not a list")
			self.assertTrue(errors)
			self.assertIn("project-sync:", stderr)

	def test_effective_reports_project_binding_and_sources(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			repo = os.path.join(cwd, "repo")
			os.makedirs(os.path.join(repo, ".git"))
			path = os.path.join(repo, ".project-sync.toml")
			write(path, '[project]\nname = "oss-sync"\nlinear = "OSS"\n')
			sub = os.path.join(repo, "sub")
			os.makedirs(sub)
			rc, payload, _ = self.effective(sub, home)
			self.assertEqual(rc, 0)
			project = payload["project"]
			if not isinstance(project, dict):
				self.fail("project is not an object")
			self.assertEqual(project["name"], "oss-sync")
			self.assertEqual(project["linear"], "OSS")
			self.assertEqual(project["source"], os.path.realpath(path))
			self.assertEqual(payload["worktree_root"], os.path.realpath(repo))
			sources = payload["sources"]
			if not isinstance(sources, dict):
				self.fail("sources is not an object")
			self.assertEqual(sources["project"], [os.path.realpath(path)])

	# --- raw stdin handling ------------------------------------------------

	def test_invalid_json_stays_silent(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			result = self.run_hook(stdin_text="not json {", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")
			self.assertEqual(result.stderr, "")

	def test_empty_stdin_stays_silent(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			result = self.run_hook(stdin_text="", cwd=cwd, home=home)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")
			self.assertEqual(result.stderr, "")

	def test_non_object_json_stays_silent(self):
		for payload in (
			"[]",
			'[{"hook_event_name": "SessionStart"}]',
			"42",
			'"text"',
			"null",
			"true",
		):
			with (
				self.subTest(payload=payload),
				tempfile.TemporaryDirectory() as home,
				tempfile.TemporaryDirectory() as cwd,
			):
				result = self.run_hook(stdin_text=payload, cwd=cwd, home=home)
				self.assertEqual(result.returncode, 0)
				self.assertEqual(result.stdout, "")
				self.assertEqual(result.stderr, "")

	# --- event filtering -----------------------------------------------------

	def test_non_session_events_stay_silent(self):
		for event in ("UserPromptSubmit", "PreToolUse", "Stop", "sessionstart"):
			with (
				self.subTest(event=event),
				tempfile.TemporaryDirectory() as home,
				tempfile.TemporaryDirectory() as cwd,
			):
				payload = json.dumps({"hook_event_name": event, "source": "startup"})
				result = self.run_hook(stdin_text=payload, cwd=cwd, home=home)
				self.assertEqual(result.returncode, 0)
				self.assertEqual(result.stdout, "")
				self.assertEqual(result.stderr, "")

	def test_missing_or_mistyped_event_stays_silent(self):
		for payload in (
			json.dumps({"source": "startup"}),
			json.dumps({"hook_event_name": 42, "source": "startup"}),
			json.dumps({"hook_event_name": ["SessionStart"], "source": "startup"}),
			json.dumps({"hook_event_name": "", "source": "startup"}),
		):
			with (
				self.subTest(payload=payload),
				tempfile.TemporaryDirectory() as home,
				tempfile.TemporaryDirectory() as cwd,
			):
				result = self.run_hook(stdin_text=payload, cwd=cwd, home=home)
				self.assertEqual(result.returncode, 0)
				self.assertEqual(result.stdout, "")
				self.assertEqual(result.stderr, "")

	def test_missing_or_unknown_source_stays_silent(self):
		for payload in (
			json.dumps({"hook_event_name": "SessionStart"}),
			json.dumps({"hook_event_name": "SessionStart", "source": "bogus"}),
			json.dumps({"hook_event_name": "SessionStart", "source": 42}),
		):
			with (
				self.subTest(payload=payload),
				tempfile.TemporaryDirectory() as home,
				tempfile.TemporaryDirectory() as cwd,
			):
				result = self.run_hook(stdin_text=payload, cwd=cwd, home=home)
				self.assertEqual(result.returncode, 0)
				self.assertEqual(result.stdout, "")
				self.assertEqual(result.stderr, "")

	# --- output protocol -------------------------------------------------------

	def test_session_start_output_matches_protocol(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			result = self.run_hook(
				stdin_text=self.session_input(cwd), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stderr, "")
			# stdout carries exactly one JSON document and nothing else.
			self.assertEqual(result.stdout.count("\n"), 1)
			payload = json.loads(result.stdout)
			self.assertEqual(set(payload), {"hookSpecificOutput"})
			specific = payload["hookSpecificOutput"]
			self.assertEqual(set(specific), {"hookEventName", "additionalContext"})
			self.assertEqual(specific["hookEventName"], "SessionStart")
			context = specific["additionalContext"]
			if not isinstance(context, str):
				self.fail("additionalContext is not a string")
			self.assertTrue(context.strip())

	def test_resume_and_clear_sources_inject(self):
		for source in ("resume", "clear"):
			with (
				self.subTest(source=source),
				tempfile.TemporaryDirectory() as home,
				tempfile.TemporaryDirectory() as cwd,
			):
				context = self.hook_context(cwd, home, source=source)
				self.assertIn("Project-sync session reminder", context)

	# --- error diagnostics never touch stdout ----------------------------------

	def test_damaged_global_config_diagnostics_stay_on_stderr(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			gdir = os.path.join(home, ".config", "project-sync")
			os.makedirs(gdir)
			write(os.path.join(gdir, "config.toml"), "[[channel]\n")
			result = self.run_hook(
				stdin_text=self.session_input(cwd), cwd=cwd, home=home
			)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(result.stdout, "")
			self.assertIn("project-sync:", result.stderr)
			self.assertIn("config.toml", result.stderr)

	def test_unreadable_config_diagnostics_stay_on_stderr(self):
		if hasattr(os, "geteuid") and os.geteuid() == 0:
			self.skipTest("permission checks are bypassed as root")
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			path = os.path.join(cwd, ".project-sync.toml")
			write(path, '[[channel]]\npurpose = "x"\ntool = "y"\n')
			os.chmod(path, 0o000)
			try:
				hook = self.run_hook(
					stdin_text=self.session_input(cwd), cwd=cwd, home=home
				)
				check = self.run_hook("--check", cwd=cwd, home=home)
				effective = self.run_hook("--effective", cwd=cwd, home=home)
			finally:
				os.chmod(path, 0o600)
			# Hook mode: fail closed with stdout protocol untouched.
			self.assertEqual(hook.returncode, 0)
			self.assertEqual(hook.stdout, "")
			self.assertIn("permission", hook.stderr)
			# --check mode: non-zero exit, human diagnostic on stderr only.
			self.assertEqual(check.returncode, 1)
			self.assertEqual(check.stdout, "")
			self.assertIn("permission", check.stderr)
			# --effective mode: stdout stays a valid JSON envelope (ok=false).
			self.assertEqual(effective.returncode, 1)
			payload = json.loads(effective.stdout)
			self.assertIs(payload["ok"], False)
			self.assertEqual(payload["channels"], [])
			self.assertIn("permission", effective.stderr)

	def test_unknown_arguments_exit_with_usage(self):
		with (
			tempfile.TemporaryDirectory() as home,
			tempfile.TemporaryDirectory() as cwd,
		):
			for args in (("--bogus",), ("--check", "a", "b")):
				with self.subTest(args=args):
					result = self.run_hook(*args, cwd=cwd, home=home)
					self.assertEqual(result.returncode, 2)
					self.assertEqual(result.stdout, "")
					self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
	unittest.main()
