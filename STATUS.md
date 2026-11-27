# Project Sync status

Assessment date: 2026-11-26. Branch: `oss-347-ci-process-tests`, cut from
`oss-346-unified-config-sync` at `98e2eec`. Earlier configuration-behavior
findings (2026-10-07) and lifecycle/cross-framework source review (2026-10-08)
live in the assessment history; this file records the OSS-347 state.

## OSS-347 delivered

- `pyproject.toml` now pins the whole quality configuration: `requires-python
  >= 3.11`, Ruff target `py311` with an explicit rule selection (tabs stay the
  house indent style; line length is owned by the formatter), and `ty`
  `python-version` + first-party search path for the hook module.
- `requirements-dev.txt` pins the validated toolchain (`ruff==0.6.7`,
  `ty==0.0.91`) so fresh checkouts and CI run identical gates.
- The test suite gained real-subprocess hook acceptance coverage: invalid
  JSON, empty stdin, non-object JSON (array/number/string/null/bool), invalid
  or mistyped events and sources, the exact `SessionStart` output protocol,
  damaged and permission-denied configs (diagnostics on stderr only — stdout
  protocol never polluted), and CLI usage errors. Prior low-frequency event
  tests are unchanged.
- `.github/workflows/ci.yml` runs unittest, `ruff check`,
  `ruff format --check`, and `ty check` on Python 3.11–3.13. No background
  processes, no per-prompt hooks.
- README install instructions no longer carry pre-release placeholders, and
  the cross-CLI section is an evidence-tiered matrix.

## Validation on this branch

| Command | Result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | 71 tests passed |
| `ruff check .` | Passed |
| `ruff format --check .` | Passed |
| `ty check .` | Passed |
| `actionlint .github/workflows/ci.yml` | Passed |

Environment: macOS, Python 3.15.2, ruff 0.6.7, ty 0.0.91, actionlint 1.7.12.

## Cross-CLI evidence record

Levels: **process-verified** (local subprocess contract only — never proof of
host context injection), **contract-checked** (docs/upstream source only),
**host-pending** (needs a real host install).

- **Claude Code** — hook process contract process-verified here; context
  injection documented and previously confirmed on Claude Code 2.1.x; skill
  model-invocable. Full support.
- **Codex CLI** — process contract verified locally. Contract-checked at
  upstream tag `rust-v0.160.1` (`hooks/src/events/session_start.rs` accepts
  nested `hookSpecificOutput.additionalContext`; source enum covers
  `startup`/`resume`/`clear`/`compact`/`fork`). Plugin discovery and
  model-visible injection remain host-pending.
- **Copilot CLI** — process contract verified locally. Contract-checked
  against the hooks reference with known deltas: a top-level `additionalContext`
  (ours is nested) and a `new` SessionStart source our allowlist rejects.
  Host-pending.
- **DSH (DeepSeek Harness)** — no hook adapter. Contract-checked at upstream
  commit `5badb15`: skill-filesystem shared/`customSkillDirs` roots can host
  the existing skill; `systemPrompt.section` can carry stable policy. Runtime
  not installed locally; no lifecycle parity promised. Host-pending.

No host context-injection test was performed in OSS-347. Process-level success
is not evidence of host injection; on non-Claude CLIs the skill remains the
reliable path. Nothing in this branch enables or modifies any local user-global
plugin configuration.
