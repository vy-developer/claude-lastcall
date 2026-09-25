#!/usr/bin/env python3
"""Model -> window: the `windows` map, learned windows, and the hook launcher.

Claude Code transcripts never record the window, so a zero-config 1M session
used to be judged against an assumed 200K until its own tokens proved
otherwise — early, advisory warnings on every new session. The `windows` map
lets the user say it once per model, and learned windows let one session's
proof carry over to the next.

Also here: scripts/lastcall-hook, the POSIX sh launcher that finds a working
Python under a desktop app's minimal PATH.

Fixtures are synthetic; every path is a temporary directory.
"""

import io
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "lastcall")
SCRIPTS = os.path.join(PLUGIN, "scripts")
LAUNCHER = os.path.join(SCRIPTS, "lastcall-hook")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(PLUGIN, "lib"))

_HOME = tempfile.mkdtemp(prefix="lastcall-home-")
os.environ["LASTCALL_HOME"] = _HOME

from lastcall_core import windows as W  # noqa: E402
from lastcall_core.agents import get_agent  # noqa: E402
from lastcall_core.agents.base import Usage  # noqa: E402
from lastcall_core.config import DEFAULTS, load_config, validate  # noqa: E402
from lastcall_core.doctor import doctor, windows_report  # noqa: E402
from lastcall_core.engine import handle_event  # noqa: E402
from lastcall_core.state import (SessionLock, prune_state, read_state,  # noqa: E402
                                 update_state)
from lastcall_core.zones import (effective_window, resolve_window,  # noqa: E402
                                 session_window)

ONE_M = 1_000_000


def usage(tokens, model="claude-opus-5-5", agent="claude", window=None,
          source="unknown", session="s1"):
    return Usage(tokens=tokens, window=window, window_source=source, model=model,
                 compacted=False, session_id=session, agent=agent)


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lastcall-windows-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state_dir = os.path.join(self.dir, "state")
        # Nothing here may read the real ~/.claude/settings.json.
        self.env = {"CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude"),
                    "LASTCALL_HOME": os.path.join(self.dir, "lc-home")}
        os.makedirs(self.env["CLAUDE_CONFIG_DIR"])
        previous = os.environ.get("LASTCALL_AGENT")
        os.environ["LASTCALL_AGENT"] = "claude"
        self.addCleanup(self._restore, "LASTCALL_AGENT", previous)

    @staticmethod
    def _restore(name, value):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def config(self, **overrides):
        config = dict(DEFAULTS, state_dir=self.state_dir, _project_dir=self.dir,
                      _config_path=None)
        config.update(overrides)
        return config

    def learned(self):
        path = os.path.join(self.state_dir, "windows.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)["models"]


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

class TestMapMatching(unittest.TestCase):
    def test_exact_beats_prefix(self):
        windows = {"claude-opus": 200_000, "claude-opus-5-5": ONE_M}
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-5-5"),
                         (ONE_M, "claude-opus-5-5"))

    def test_longest_prefix_wins(self):
        windows = {"claude": 200_000, "claude-opus-5": ONE_M}
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-5-5")[0], ONE_M)
        self.assertEqual(W.match_window(windows, "claude", "claude-sonnet-5")[0], 200_000)

    def test_glob_matches_the_whole_model(self):
        windows = {"claude-*-5": ONE_M}
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-5")[0], ONE_M)
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-5-1")[0], None)

    def test_exact_beats_glob_and_longer_glob_beats_shorter(self):
        windows = {"claude-*": 200_000, "claude-opus-*": ONE_M, "claude-opus-5": 400_000}
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-5")[0], 400_000)
        # "claude-opus-5" is also a prefix of claude-opus-5-5, and a longer one.
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-5-5")[0], 400_000)
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-4-1")[0], ONE_M)
        self.assertEqual(W.match_window(windows, "claude", "claude-haiku-4-5")[0], 200_000)

    def test_agent_qualified_beats_bare_and_other_agents_are_ignored(self):
        windows = {"opus": 200_000, "claude:opus": ONE_M, "codex:gpt-5": 400_000}
        self.assertEqual(W.match_window(windows, "claude", "opus"), (ONE_M, "claude:opus"))
        self.assertEqual(W.match_window(windows, "codex", "opus"), (200_000, "opus"))
        self.assertEqual(W.match_window(windows, "claude", "gpt-5")[0], None)

    def test_case_insensitive(self):
        self.assertEqual(W.match_window({"Claude-Opus": ONE_M}, "claude", "claude-opus-5")[0], ONE_M)

    def test_brackets_are_literal_and_a_1m_model_needs_a_1m_key(self):
        windows = {"claude-opus-5-5": 200_000, "claude-opus-5-5[1m]": ONE_M}
        self.assertEqual(W.match_window(windows, "claude", "claude-opus-5-5[1m]")[0], ONE_M)
        # A model that names its window is not claimed by a key that does not.
        self.assertEqual(W.match_window({"claude-opus": 200_000}, "claude",
                                        "claude-opus-5-5[1m]")[0], None)

    def test_no_match_no_map_no_model(self):
        self.assertEqual(W.match_window({"gpt": 1}, "claude", "claude-opus-5"), (None, None))
        self.assertEqual(W.match_window(None, "claude", "claude-opus-5"), (None, None))
        self.assertEqual(W.match_window({"claude": 1}, "claude", None), (None, None))


class TestMapValidation(Case):
    def test_bad_values_are_dropped_one_by_one_and_reported(self):
        config = self.config(windows={"a": 0, "b": -5, "c": "1m", "d": True,
                                      "e": 200_000, "_comment": "x"})
        problems = validate(config)
        self.assertEqual(config["windows"], {"e": 200_000})
        text = "\n".join(problems)
        for key in ("a", "b", "c", "d"):
            self.assertIn('windows["%s"]' % key, text)
        self.assertNotIn("_comment", text)

    def test_wrong_shape_is_dropped_whole(self):
        config = self.config(windows=[["claude-opus-5", ONE_M]])
        problems = validate(config)
        self.assertIsNone(config["windows"])
        self.assertTrue(any(p.startswith("windows should be") for p in problems))

    def test_an_unknown_agent_qualifier_is_reported(self):
        config = self.config(windows={"gemini:pro": ONE_M,
                                      "us.anthropic.claude-opus-5-v1:0": ONE_M})
        problems = validate(config)
        self.assertEqual(len(problems), 1)
        self.assertIn('"gemini" is not an agent', problems[0])

    def test_global_and_project_maps_merge(self):
        home = self.env["LASTCALL_HOME"]
        os.makedirs(home)
        with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as handle:
            json.dump({"windows": {"claude-opus": ONE_M, "claude-sonnet": 200_000}}, handle)
        project = os.path.join(self.dir, "project")
        os.makedirs(os.path.join(project, ".git"))
        with open(os.path.join(project, ".lastcall.json"), "w", encoding="utf-8") as handle:
            json.dump({"windows": {"claude-sonnet": ONE_M}}, handle)
        config = load_config({"cwd": project}, self.env)
        self.assertEqual(config["windows"], {"claude-opus": ONE_M, "claude-sonnet": ONE_M})

    def test_environment_takes_json(self):
        env = dict(self.env, LASTCALL_WINDOWS='{"claude-opus": 1000000}')
        config = load_config({"cwd": self.dir}, env)
        self.assertEqual(config["windows"], {"claude-opus": ONE_M})


# --------------------------------------------------------------------------
# Resolution order
# --------------------------------------------------------------------------

class TestResolutionOrder(Case):
    MODEL = "claude-opus-5-5"

    def resolve(self, config, state=None, use=None, learned=None, settings=None):
        return resolve_window(config, state or {}, use or usage(90_000, self.MODEL),
                              "claude", learned=learned, settings=settings)

    def learned_map(self, window=ONE_M):
        return {W.learned_key("claude", self.MODEL): {"window": window, "source": "evidence"}}

    def test_config_beats_everything(self):
        config = self.config(context_window_tokens=400_000, windows={self.MODEL: ONE_M})
        self.assertEqual(self.resolve(config, {"window_from_statusline": ONE_M},
                                      learned=self.learned_map()), (400_000, "config"))

    def test_statusline_beats_the_map(self):
        config = self.config(windows={self.MODEL: 200_000})
        self.assertEqual(self.resolve(config, {"window_from_statusline": ONE_M}),
                         (ONE_M, "statusline"))

    def test_map_beats_learned(self):
        config = self.config(windows={self.MODEL: 400_000})
        self.assertEqual(self.resolve(config, learned=self.learned_map()), (400_000, "map"))

    def test_learned_beats_the_model_name_and_settings(self):
        use = usage(90_000, self.MODEL + "[1m]", window=ONE_M, source="model-name")
        learned = {W.learned_key("claude", self.MODEL + "[1m]"): {"window": 400_000}}
        self.assertEqual(self.resolve(self.config(), use=use, learned=learned),
                         (400_000, "learned"))
        self.assertEqual(self.resolve(self.config(), learned=self.learned_map(),
                                      settings=lambda: (ONE_M, "configured model x")),
                         (ONE_M, "learned"))

    def test_model_name_beats_settings_beats_evidence(self):
        use = usage(90_000, self.MODEL + "[1m]", window=ONE_M, source="model-name")
        self.assertEqual(self.resolve(self.config(), use=use,
                                      settings=lambda: (400_000, "configured")),
                         (ONE_M, "model-name"))
        self.assertEqual(self.resolve(self.config(), {"max_observed": 300_000},
                                      settings=lambda: (ONE_M, "configured model x")),
                         (ONE_M, "configured model x"))
        window, source = self.resolve(self.config(), {"max_observed": 300_000})
        self.assertEqual(window, ONE_M)
        self.assertIn("proven by 300,000", source)

    def test_nothing_known_falls_back_to_assumed(self):
        config = self.config()
        state = {}
        window, source, assumed = effective_window(config, state, usage(90_000),
                                                   "claude", self.env, learned={})
        self.assertEqual((window, source, assumed), (200_000, "assumed", True))

    def test_learned_is_read_lazily_and_only_when_reached(self):
        calls = []
        config = self.config(windows={self.MODEL: ONE_M})
        self.resolve(config, learned=lambda: calls.append(1) or {})
        self.assertEqual(calls, [])
        self.resolve(self.config(), learned=lambda: calls.append(1) or {})
        self.assertEqual(calls, [1])

    def test_every_source_is_corrected_when_disproven(self):
        observed = {"max_observed": 450_000}
        cases = [
            (self.config(windows={self.MODEL: 200_000}), {}, None, "map says 200,000"),
            (self.config(), {}, self.learned_map(200_000), "learned says 200,000"),
            (self.config(), {"window_from_statusline": 200_000}, None,
             "statusline says 200,000"),
            (self.config(context_window_tokens=200_000), {}, None, "config says 200,000"),
        ]
        for config, state, learned, expected in cases:
            state = dict(state, **observed)
            window, source = self.resolve(config, state, learned=learned)
            self.assertEqual(window, ONE_M, expected)
            self.assertIn(expected, source)
            self.assertIn("450,000", source)
        window, source = self.resolve(self.config(), observed,
                                      settings=lambda: (200_000, "configured model"))
        self.assertIn("configured model says 200,000", source)

    def test_codex_always_uses_the_rollout(self):
        use = usage(50_000, "gpt-test", agent="codex", window=258_400, source="transcript")
        config = self.config(windows={"gpt-test": ONE_M, "codex:gpt": ONE_M})
        learned = {W.learned_key("codex", "gpt-test"): {"window": ONE_M}}
        self.assertEqual(resolve_window(config, {}, use, "codex", learned=learned),
                         (258_400, "transcript"))

    def test_codex_without_a_rollout_window_can_use_the_map(self):
        use = usage(50_000, "gpt-test", agent="codex")
        config = self.config(windows={"codex:gpt-": 400_000})
        self.assertEqual(resolve_window(config, {}, use, "codex"), (400_000, "map"))


# --------------------------------------------------------------------------
# Learning
# --------------------------------------------------------------------------

def _transcript(path, tokens, model="claude-opus-5-5", session="s1"):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "assistant", "sessionId": session, "isSidechain": False,
            "message": {"id": "msg_%d" % tokens, "model": model, "usage": {
                "input_tokens": 10, "cache_read_input_tokens": tokens - 10,
                "cache_creation_input_tokens": 0, "output_tokens": 5}}}) + "\n")


class TestLearning(Case):
    def hook(self, config, event, transcript, session="s1"):
        out = io.StringIO()
        handle_event(event, {"session_id": session, "transcript_path": transcript,
                             "hook_event_name": event, "cwd": self.dir},
                     env=self.env, out=out, config=config, agent=get_agent("claude"))
        return out.getvalue()

    def test_tokens_beyond_200k_teach_the_model_1m(self):
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 250_000)
        self.hook(self.config(), "PostToolUse", transcript)
        entry = self.learned()["claude:claude-opus-5-5"]
        self.assertEqual((entry["window"], entry["source"]), (ONE_M, "evidence"))
        self.assertEqual(entry["model"], "claude-opus-5-5")
        self.assertIsInstance(entry["at"], int)

    def test_a_small_session_teaches_nothing(self):
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 90_000)
        self.hook(self.config(), "PostToolUse", transcript)
        self.assertEqual(self.learned(), {})

    def test_claims_are_never_learned(self):
        """config, the map and settings are what someone said, not what a
        session proved: learning them would spread one wrong setting."""
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 90_000)
        self.hook(self.config(context_window_tokens=ONE_M), "PostToolUse", transcript)
        self.hook(self.config(windows={"claude": ONE_M}), "Stop", transcript)
        self.assertEqual(self.learned(), {})

    def test_the_status_line_window_is_learned_for_the_transcript_model(self):
        config = self.config()
        update_state(config, "s1", {"window_from_statusline": 200_000}, "claude")
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 90_000, model="claude-sonnet-5")
        self.hook(config, "PostToolUse", transcript)
        entry = self.learned()["claude:claude-sonnet-5"]
        self.assertEqual((entry["window"], entry["source"]), (200_000, "statusline"))

    def test_the_status_line_script_learns_the_model_it_is_shown(self):
        env = dict(os.environ, LASTCALL_HOME=self.env["LASTCALL_HOME"],
                   LASTCALL_STATE_DIR=self.state_dir)
        subprocess.run([sys.executable, os.path.join(SCRIPTS, "statusline.py")],
                       input=json.dumps({"session_id": "s9", "cwd": self.dir,
                                         "model": {"id": "claude-opus-5-5[1m]",
                                                   "display_name": "Opus"},
                                         "context_window_size": ONE_M}).encode(),
                       stdout=subprocess.PIPE, env=env, cwd=self.dir, check=True)
        entry = self.learned()["claude:claude-opus-5-5"]
        self.assertEqual((entry["window"], entry["source"]), (ONE_M, "statusline"))

    def test_learned_once_per_session_not_on_every_tool_call(self):
        transcript = os.path.join(self.dir, "s1.jsonl")
        config = self.config()
        _transcript(transcript, 250_000)
        self.hook(config, "PostToolUse", transcript)
        path = os.path.join(self.state_dir, "windows.json")
        os.utime(path, (1, 1))
        for tokens in (260_000, 270_000):
            _transcript(transcript, tokens)
            self.hook(config, "PostToolUse", transcript)
        self.assertEqual(os.stat(path).st_mtime, 1)

    def test_a_learned_window_is_used_by_the_next_session(self):
        config = self.config()
        first = os.path.join(self.dir, "s1.jsonl")
        _transcript(first, 250_000, session="s1")
        self.hook(config, "PostToolUse", first, session="s1")
        second = os.path.join(self.dir, "s2.jsonl")
        _transcript(second, 90_000, session="s2")
        self.assertEqual(self.hook(config, "PostToolUse", second, session="s2"), "")
        state = read_state(config, "s2", "claude")
        self.assertEqual((state["window"], state["window_source"]), (ONE_M, "learned"))

    def test_codex_rollout_windows_are_recorded_for_display(self):
        config = self.config()
        state = {}
        use = usage(50_000, "gpt-test", agent="codex", window=258_400, source="transcript")
        self.assertTrue(W.learn(config, "codex", state, use))
        entry = self.learned()["codex:gpt-test"]
        self.assertEqual((entry["window"], entry["source"]), (258_400, "rollout"))

    def test_latest_proof_wins_and_conflicts_are_kept(self):
        config = self.config()
        W.record_learned(config, "claude", "claude-opus-5", 200_000, "statusline", now=100)
        W.record_learned(config, "claude", "claude-opus-5", ONE_M, "evidence", now=200)
        entry = self.learned()["claude:claude-opus-5"]
        self.assertEqual(entry["window"], ONE_M)
        self.assertEqual(entry["seen"], {"200000": 100, "1000000": 200})

    def test_pruning_never_removes_learned_windows(self):
        config = self.config(state_ttl_days=1)
        W.record_learned(config, "claude", "claude-opus-5", ONE_M, "evidence")
        path = os.path.join(self.state_dir, "windows.json")
        os.utime(path, (1, 1))
        prune_state(config)
        self.assertTrue(os.path.exists(path))

    def test_a_corrupt_file_is_ignored_and_then_repaired(self):
        config = self.config()
        os.makedirs(self.state_dir)
        with open(os.path.join(self.state_dir, "windows.json"), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(W.load_learned(config), {})
        self.assertTrue(W.record_learned(config, "claude", "m", ONE_M, "evidence"))
        self.assertEqual(self.learned()["claude:m"]["window"], ONE_M)


def _learn_many(state_dir, worker, count, lock_timeout=60):
    # A loaded CI runner can keep a writer waiting past the hook's two
    # seconds, and then it skips (by design). Here every writer must get its
    # turn, so any entry missing was lost to a race, not to the timeout.
    W.LOCK_TIMEOUT = lock_timeout
    config = dict(DEFAULTS, state_dir=state_dir)
    for index in range(count):
        if not W.record_learned(config, "claude", "model-%d-%d" % (worker, index),
                                ONE_M, "evidence"):
            raise AssertionError("worker %d could not record entry %d" % (worker, index))


class TestConcurrentLearning(Case):
    def setUp(self):
        super().setUp()
        original = W.LOCK_TIMEOUT
        self.addCleanup(setattr, W, "LOCK_TIMEOUT", original)

    def test_processes_learning_at_once_lose_nothing(self):
        context = multiprocessing.get_context("spawn")
        workers = [context.Process(target=_learn_many, args=(self.state_dir, n, 10))
                   for n in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(120)
            self.assertEqual(worker.exitcode, 0)
        learned = self.learned()
        self.assertEqual(len(learned), 60)
        leftovers = [n for n in os.listdir(self.state_dir) if ".tmp" in n]
        self.assertEqual(leftovers, [])

    def test_threads_learning_at_once_lose_nothing(self):
        errors = []

        def work(n):
            try:
                _learn_many(self.state_dir, n, 10)
            except AssertionError as error:
                errors.append(error)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.learned()), 40)

    def test_a_held_lock_skips_the_write_instead_of_racing_it(self):
        """The old lock gave up after a second and wrote anyway, so a slow
        holder's entry could be overwritten (CI lost 1 of 60). Past the
        timeout the proof is skipped; the file is left as the holder wrote it."""
        config = self.config()
        self.assertTrue(W.record_learned(config, "claude", "held", ONE_M, "evidence"))
        W.LOCK_TIMEOUT = 0.05
        with SessionLock(W.learned_path(config) + ".lock", timeout=1) as holder:
            self.assertTrue(holder.acquired)
            self.assertFalse(W.record_learned(config, "claude", "late", ONE_M, "evidence"))
        self.assertEqual(set(self.learned()), {"claude:held"})
        self.assertTrue(W.record_learned(config, "claude", "late", ONE_M, "evidence"))
        self.assertEqual(set(self.learned()), {"claude:held", "claude:late"})


# --------------------------------------------------------------------------
# doctor and the status helper
# --------------------------------------------------------------------------

class TestDoctorAndStatus(Case):
    def run_doctor(self, argv, env):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.project)
        try:
            with redirect_stdout(out):
                code = doctor(argv, env=env)
        finally:
            os.chdir(cwd)
        return code, out.getvalue()

    def setUp(self):
        super(TestDoctorAndStatus, self).setUp()
        self.project = os.path.join(self.dir, "project")
        os.makedirs(os.path.join(self.project, ".git"))

    def write_config(self, data):
        with open(os.path.join(self.project, ".lastcall.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_windows_section_lists_the_map_and_what_was_learned(self):
        self.write_config({"windows": {"claude-opus-5-5": ONE_M, "bad": 0}})
        config = load_config({"cwd": self.project}, self.env)
        W.record_learned(config, "claude", "claude-sonnet-5", 200_000, "statusline")
        W.record_learned(config, "claude", "claude-sonnet-5", ONE_M, "evidence")
        code, out = self.run_doctor([], self.env)
        self.assertEqual(code, 0)
        self.assertIn("windows (model -> context window)", out)
        self.assertIn("map      claude-opus-5-5", out)
        self.assertIn("1,000,000", out)
        self.assertIn("learned  claude:claude-sonnet-5", out)
        self.assertIn("from evidence", out)
        self.assertIn("also seen: 200,000", out)
        self.assertIn('PROBLEM       : windows["bad"]', out)

    def test_windows_section_says_when_there_is_nothing(self):
        lines = "\n".join(windows_report(self.config()))
        self.assertIn("map      (none", lines)
        self.assertIn("learned  (none yet", lines)

    def test_doctor_names_the_map_entry_a_session_matched(self):
        self.write_config({"windows": {"claude-opus": ONE_M}})
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 90_000)
        code, out = self.run_doctor([transcript], self.env)
        self.assertEqual(code, 0, out)
        self.assertIn("1,000,000 tokens (map)", out)
        self.assertIn('matched windows["claude-opus"]', out)
        self.assertIn("band          : GREEN", out)

    def test_doctor_says_where_a_learned_window_came_from(self):
        config = load_config({"cwd": self.project}, self.env)
        W.record_learned(config, "claude", "claude-opus-5-5", ONE_M, "evidence")
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 90_000)
        code, out = self.run_doctor([transcript], self.env)
        self.assertIn("window source : learned", out)
        self.assertIn("learned from evidence on", out)

    def test_session_window_is_what_the_hooks_would_use(self):
        config = self.config()
        W.record_learned(config, "claude", "claude-opus-5-5", ONE_M, "evidence")
        # Learned from another session: assumed until this one proves it.
        self.assertEqual(session_window(usage(90_000), config, env=self.env),
                         (ONE_M, "learned", True))
        self.assertEqual(session_window(usage(250_000), config, env=self.env),
                         (ONE_M, "learned", False))
        update_state(config, "s1", {"window_from_statusline": 400_000}, "claude")
        self.assertEqual(session_window(usage(90_000), config, env=self.env),
                         (400_000, "statusline", False))
        codex = usage(50_000, "gpt-test", agent="codex", window=258_400,
                      source="transcript")
        self.assertEqual(session_window(codex, config, env=self.env),
                         (258_400, "transcript", False))
        self.assertEqual(session_window(None, config), (None, "unknown", False))

    def test_session_window_falls_back_like_the_hooks(self):
        self.assertEqual(session_window(usage(90_000, "claude-new-9"), self.config(),
                                        env=self.env),
                         (200_000, "assumed", True))


def _boundary(path, pre_tokens, post_tokens=20_000, session="s1", trigger="auto", uuid="b-1"):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "system", "subtype": "compact_boundary", "sessionId": session,
            "isSidechain": False, "uuid": uuid, "timestamp": "2026-09-25T10:00:00.000Z",
            "compactMetadata": {"trigger": trigger, "preTokens": pre_tokens,
                                "postTokens": post_tokens}}) + "\n")


class TestLearnedIsNotProof(Case):
    """Review finding: one 1M session taught claude:<model> = 1M, and every
    later 200K session on the same model id was judged against 1M and never
    warned — learning only ever corrected upwards."""

    MODEL = "claude-opus-5-5"

    def hook(self, config, event, transcript, session="s1"):
        out = io.StringIO()
        handle_event(event, {"session_id": session, "transcript_path": transcript,
                             "hook_event_name": event, "cwd": self.dir},
                     env=self.env, out=out, config=config, agent=get_agent("claude"))
        return out.getvalue()

    def test_a_learned_1m_is_assumed_until_this_session_proves_it(self):
        config = self.config()
        W.record_learned(config, "claude", self.MODEL, ONE_M, "evidence")
        self.assertEqual(effective_window(config, {}, usage(90_000), "claude", self.env),
                         (ONE_M, "learned", True))
        self.assertEqual(effective_window(config, {}, usage(250_000), "claude", self.env),
                         (ONE_M, "learned", False))
        # A learned 200K is no claim of headroom; a user's map entry is the user's.
        W.record_learned(config, "claude", "claude-sonnet-5", 200_000, "statusline")
        self.assertFalse(effective_window(config, {}, usage(90_000, "claude-sonnet-5"),
                                          "claude", self.env)[2])
        mapped = self.config(windows={self.MODEL: ONE_M})
        self.assertEqual(effective_window(mapped, {}, usage(90_000), "claude", self.env),
                         (ONE_M, "map", False))

    def test_a_learned_window_never_blocks_and_says_where_it_came_from(self):
        config = self.config(mode="block_once", zones=[
            {"name": "yellow", "at": 10}, {"name": "red", "at": 15, "block": True}])
        W.record_learned(config, "claude", self.MODEL, ONE_M, "evidence")
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 160_000)
        output = json.loads(self.hook(config, "Stop", transcript))
        self.assertNotIn("decision", output)
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("LEARNED from another session", text)
        self.assertNotIn("assumed the\nstandard", text)

    def test_an_auto_compaction_far_below_the_learned_window_ends_the_trust(self):
        config = self.config()
        W.record_learned(config, "claude", self.MODEL, ONE_M, "evidence")
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 150_000)
        self.hook(config, "PostToolUse", transcript)
        _boundary(transcript, 160_000)
        self.hook(config, "PostToolUse", transcript)
        entry = self.learned()["claude:%s" % self.MODEL]
        self.assertEqual(entry["conflict"]["pre_tokens"], 160_000)
        # The next session on that model: the assumed fallback, not the 1M.
        self.assertEqual(effective_window(config, {}, usage(90_000, session="s2"), "claude",
                                          self.env), (200_000, "assumed", True))
        # A later 1M proof does not clear it: only the user's map settles it.
        W.record_learned(config, "claude", self.MODEL, ONE_M, "evidence")
        self.assertIn("conflict", self.learned()["claude:%s" % self.MODEL])
        mapped = self.config(windows={self.MODEL: ONE_M})
        self.assertEqual(effective_window(mapped, {}, usage(90_000), "claude", self.env),
                         (ONE_M, "map", False))
        lines = "\n".join(windows_report(config))
        self.assertIn("CONFLICT", lines)
        self.assertIn('pin the model in "windows"', lines)

    def test_manual_or_near_the_window_compactions_prove_nothing(self):
        config = self.config()
        W.record_learned(config, "claude", self.MODEL, ONE_M, "evidence")
        transcript = os.path.join(self.dir, "s1.jsonl")
        _transcript(transcript, 150_000)
        _boundary(transcript, 150_000, trigger="manual", uuid="b-1")
        self.hook(config, "PostToolUse", transcript)
        _transcript(transcript, 160_000)
        _boundary(transcript, 900_000, uuid="b-2")
        self.hook(config, "PostToolUse", transcript)
        self.assertNotIn("conflict", self.learned()["claude:%s" % self.MODEL])


# --------------------------------------------------------------------------
# End to end: the real hook script
# --------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hooks_e2e import SID, SID2, HookCase  # noqa: E402


class TestLearnedWindowsEndToEnd(HookCase):
    MODEL = "claude-opus-5-5"

    def test_zero_config_1m_session_on_a_learned_model_gets_no_early_warning(self):
        # An earlier session on this model proved 1M.
        self.claude_reply(250_000, model=self.MODEL, session=SID2)
        self.run_hook("PostToolUse", self.claude_payload("PostToolUse", session=SID2))
        # A fresh zero-config session on the same model at 90K: 9% of 1M,
        # not 45% of an assumed 200K.
        self.claude_reply(90_000, model=self.MODEL)
        for event in ("PostToolUse", "UserPromptSubmit"):
            self.assertIsNone(self.run_hook(event, self.claude_payload(event)))
        state = self.state("claude")
        self.assertEqual((state["window"], state["window_source"]), (ONE_M, "learned"))

    def test_without_the_learned_window_the_same_session_is_warned(self):
        self.claude_reply(90_000, model=self.MODEL)
        output = self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        self.assertIn("assumed the", self.context_of(output, "PostToolUse"))

    def test_a_global_map_entry_does_the_same_from_the_first_session(self):
        self.configure_globally({"windows": {"claude-opus": ONE_M}})
        self.claude_reply(90_000, model=self.MODEL)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.assertEqual(self.state("claude")["window_source"], "map")

    def test_a_learned_1m_still_warns_at_its_own_zones_and_can_block(self):
        self.claude_reply(250_000, model=self.MODEL, session=SID2)
        self.run_hook("PostToolUse", self.claude_payload("PostToolUse", session=SID2))
        text = self.claude_reply(600_000, model=self.MODEL)
        output = self.run_hook("Stop", self.claude_payload("Stop", last_assistant_message=text))
        self.assertEqual(output["decision"], "block")  # not assumed: block_once applies

    def test_codex_rollout_windows_are_recorded(self):
        self.codex_tokens(50_000)
        self.run_hook("PostToolUse", self.codex_payload("PostToolUse"))
        with open(os.path.join(self.lastcall_home, "state", "windows.json"), encoding="utf-8") as handle:
            entry = json.load(handle)["models"]["codex:gpt-test"]
        self.assertEqual((entry["window"], entry["source"]), (258_400, "rollout"))


# --------------------------------------------------------------------------
# scripts/lastcall-hook
# --------------------------------------------------------------------------

FAKE = """#!/bin/sh
{ printf 'interp=%%s\\n' "%(name)s"; for a in "$@"; do printf 'arg=%%s\\n' "$a"; done
  printf 'stdin='; /bin/cat; } > "$LAUNCH_OUT"
"""


@unittest.skipIf(os.name == "nt" or not os.path.exists("/bin/sh"), "POSIX sh only")
class TestHookLauncher(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lastcall-launch-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = os.path.join(self.dir, "out.txt")
        self.path_dir = self.mkdir("path")
        self.home = self.mkdir("home")
        self.env = {
            "PATH": self.path_dir,
            "HOME": self.home,
            "LAUNCH_OUT": self.out,
            # Stand-ins for the fixed system paths.
            "_LASTCALL_HOOK_STUB": os.path.join(self.dir, "usr-bin", "python3"),
            "_LASTCALL_HOOK_XCRUN": os.path.join(self.dir, "usr-bin", "xcrun"),
            "_LASTCALL_HOOK_DEVDIRS": os.path.join(self.dir, "clt"),
            "_LASTCALL_HOOK_FIXED": "",
        }

    def mkdir(self, *parts):
        path = os.path.join(self.dir, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def fake(self, path, name=None, body=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body if body is not None else FAKE % {"name": name or path})
        os.chmod(path, 0o755)
        return path

    def launch(self, *args, stdin=b'{"hook_event_name": "Stop"}', **env):
        environment = dict(self.env)
        environment.update(env)
        result = subprocess.run(["/bin/sh", LAUNCHER] + list(args), input=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not os.path.exists(self.out):
            return None
        with open(self.out, encoding="utf-8") as handle:
            text = handle.read()
        os.remove(self.out)
        return text

    def chosen(self, *args, **env):
        text = self.launch(*(args or ("Stop",)), **env)
        return text.splitlines()[0][len("interp="):] if text else None

    def test_python3_on_path_gets_the_script_the_arguments_and_stdin(self):
        self.fake(os.path.join(self.path_dir, "python3"), "path-python3")
        text = self.launch("PostToolUse", "extra arg")
        self.assertEqual(text.splitlines(), [
            "interp=path-python3",
            "arg=" + os.path.join(SCRIPTS, "lastcall.py"),
            "arg=PostToolUse",
            "arg=extra arg",
            'stdin={"hook_event_name": "Stop"}',
        ])

    def test_lastcall_python_wins(self):
        self.fake(os.path.join(self.path_dir, "python3"), "path-python3")
        chosen = self.fake(os.path.join(self.dir, "mine", "py"), "mine")
        self.assertEqual(self.chosen(LASTCALL_PYTHON=chosen), "mine")
        self.fake(os.path.join(self.path_dir, "pypy3"), "by-name")
        self.assertEqual(self.chosen(LASTCALL_PYTHON="pypy3"), "by-name")

    def test_a_missing_lastcall_python_falls_through(self):
        self.fake(os.path.join(self.path_dir, "python3"), "path-python3")
        self.assertEqual(self.chosen(LASTCALL_PYTHON="/nope/python3"), "path-python3")

    def test_the_macos_stub_is_skipped_without_developer_tools(self):
        stub = self.fake(self.env["_LASTCALL_HOOK_STUB"], "stub")
        self.fake(self.env["_LASTCALL_HOOK_XCRUN"], body="#!/bin/sh\nexit 0\n")
        os.symlink(stub, os.path.join(self.path_dir, "python3"))
        self.fake(os.path.join(self.path_dir, "xcode-select"), body="#!/bin/sh\nexit 2\n")
        brew = self.fake(os.path.join(self.dir, "brew", "python3"), "brew")
        # PATH's python3 is a symlink to the stub, which is not the stub's own
        # path — so point PATH straight at the stub's directory as the
        # desktop app's /usr/bin would be.
        env = {"PATH": os.path.dirname(stub) + ":" + self.path_dir,
               "_LASTCALL_HOOK_FIXED": brew + ":" + stub}
        self.assertEqual(self.chosen(**env), "brew")
        # Nothing else: the stub is still not run.
        env["_LASTCALL_HOOK_FIXED"] = stub
        self.assertIsNone(self.chosen(**env))

    def test_the_stub_runs_when_the_command_line_tools_exist(self):
        stub = self.fake(self.env["_LASTCALL_HOOK_STUB"], "stub")
        self.fake(self.env["_LASTCALL_HOOK_XCRUN"], body="#!/bin/sh\nexit 0\n")
        self.fake(os.path.join(self.env["_LASTCALL_HOOK_DEVDIRS"], "usr", "bin", "python3"),
                  body="#!/bin/sh\nexit 0\n")
        self.assertEqual(self.chosen(PATH=os.path.dirname(stub)), "stub")

    def test_the_stub_runs_when_xcode_select_names_a_developer_dir(self):
        stub = self.fake(self.env["_LASTCALL_HOOK_STUB"], "stub")
        self.fake(self.env["_LASTCALL_HOOK_XCRUN"], body="#!/bin/sh\nexit 0\n")
        xcode = self.mkdir("Xcode-beta.app", "Developer")
        self.fake(os.path.join(xcode, "usr", "bin", "python3"), body="#!/bin/sh\nexit 0\n")
        self.fake(os.path.join(self.path_dir, "xcode-select"),
                  body="#!/bin/sh\necho '%s'\n" % xcode)
        env = {"PATH": self.path_dir, "_LASTCALL_HOOK_FIXED": stub}
        self.assertEqual(self.chosen(**env), "stub")

    def test_without_xcrun_usr_bin_python3_is_a_real_interpreter(self):
        stub = self.fake(self.env["_LASTCALL_HOOK_STUB"], "linux-python3")
        self.assertEqual(self.chosen(PATH=os.path.dirname(stub)), "linux-python3")

    def test_fixed_locations_are_tried_in_order(self):
        first = self.fake(os.path.join(self.dir, "a", "python3"), "a")
        second = self.fake(os.path.join(self.dir, "b", "python3"), "b")
        missing = os.path.join(self.dir, "none", "python3")
        self.assertEqual(self.chosen(_LASTCALL_HOOK_FIXED="%s:%s:%s" % (missing, second, first)),
                         "b")

    def test_the_default_list_includes_pyenv_and_conda_under_home(self):
        with open(LAUNCHER, encoding="utf-8") as handle:
            text = handle.read()
        for location in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                         "$HOME/.pyenv/shims/python3", "$CONDA_PREFIX/bin/python3",
                         "$HOME/miniconda3/bin/python3"):
            self.assertIn(location, text)

    def test_python_then_py_dash_3_are_last(self):
        self.fake(os.path.join(self.path_dir, "py"), "py-launcher")
        text = self.launch("Stop")
        self.assertEqual(text.splitlines()[:3], ["interp=py-launcher", "arg=-3",
                                                  "arg=" + os.path.join(SCRIPTS, "lastcall.py")])
        self.fake(os.path.join(self.path_dir, "python"), "plain-python")
        self.assertEqual(self.chosen(), "plain-python")

    def test_nothing_found_exits_0_silently(self):
        os.symlink("/bin/cat", os.path.join(self.path_dir, "cat"))
        result = subprocess.run(["/bin/sh", LAUNCHER, "Stop"], input=b"{}" * 50000,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.env, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_nothing_found_and_no_cat_is_still_silent(self):
        result = subprocess.run(["/bin/sh", LAUNCHER, "Stop"], input=b"{}",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.env, timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_it_runs_the_real_hook(self):
        env = dict(self.env, LASTCALL_PYTHON=sys.executable)
        result = subprocess.run(["/bin/sh", LAUNCHER, "--version"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.decode().strip(), r"^\d+\.\d+\.\d+$")

    def test_the_file_is_posix_sh_with_lf_and_executable(self):
        with open(LAUNCHER, "rb") as handle:
            data = handle.read()
        self.assertTrue(data.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", data)
        data.decode("ascii")
        self.assertTrue(os.stat(LAUNCHER).st_mode & stat.S_IXUSR)
        if shutil.which("dash"):
            subprocess.run(["dash", "-n", LAUNCHER], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
