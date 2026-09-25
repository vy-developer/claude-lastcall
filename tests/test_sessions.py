#!/usr/bin/env python3
"""Tests for lastcall_core.sessions and its CLI. Synthetic homes only.

Nothing here reads the real ~/.claude or ~/.codex: every call passes a temp
home explicitly, and liveness probes (lsof / ps) are stubbed.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "plugins", "lastcall", "lib")


def _import_lib():
    """The hook script plugins/lastcall/scripts/lastcall.py is also importable
    as `lastcall` (test_lastcall/test_docs put it on sys.path), which shadows
    the lib package. Import the package with the script module set aside,
    then put the script back so the other suites are unaffected."""
    shadow = sys.modules.pop("lastcall", None)
    sys.path.insert(0, LIB)
    try:
        import lastcall_core.cli_sessions as cli
        import lastcall_core.sessions as sessions
    finally:
        sys.path.remove(LIB)
        for name in [n for n in sys.modules if n == "lastcall_core" or n.startswith("lastcall_core.")]:
            del sys.modules[name]
        if shadow is not None:
            sys.modules["lastcall"] = shadow
    return cli, sessions


cli_sessions, S = _import_lib()

PROMPT = "Please refactor the widget loader so that it streams chunks lazily today"
DERIVED = "Please refactor the widget loader so that it…"


_REAL_OPEN_ROLLOUTS = S.codex_open_rollouts  # setUp stubs the module attribute


def read(path, mode="rb"):
    with open(path, mode, encoding=None if "b" in mode else "utf-8") as fh:
        return fh.read()


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump(obj):
    return json.dumps(obj, separators=(",", ":"))


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class Homes(unittest.TestCase):
    """Builds synthetic Claude and Codex homes plus a git repo with a worktree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lastcall-sessions-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude = os.path.join(self.tmp, "claude")
        self.codex = os.path.join(self.tmp, "codex")
        self.repo = os.path.join(self.tmp, "src", "widget")
        os.makedirs(os.path.join(self.repo, ".git", "worktrees", "wt"))
        self.worktree = os.path.join(self.tmp, "src", "widget-wt")
        os.makedirs(self.worktree)
        with open(os.path.join(self.worktree, ".git"), "w", encoding="utf-8") as fh:
            fh.write("gitdir: %s\n" % os.path.join(self.repo, ".git", "worktrees", "wt"))
        self.plain = os.path.join(self.tmp, "scratch")
        os.makedirs(self.plain)
        S.project_root.cache_clear()
        patcher = mock.patch.object(S, "codex_open_rollouts", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(S, "codex_running", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- Claude fixtures
    def transcript(self, sid, cwd=None, lines=(), entrypoint="cli", prompt=PROMPT,
                   slug="-proj", trailing_newline=True, ts="2026-09-20T10:00:00.123Z"):
        cwd = cwd or self.repo
        folder = os.path.join(self.claude, "projects", slug)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        body = [
            dump({"type": "queue-operation", "operation": "enqueue", "sessionId": sid,
                  "timestamp": ts}),
            dump({"type": "user", "sessionId": sid, "cwd": cwd, "entrypoint": entrypoint,
                  "timestamp": ts, "isMeta": True,
                  "message": {"role": "user", "content": "<command-name>/clear</command-name>"}}),
            dump({"type": "user", "sessionId": sid, "cwd": cwd, "entrypoint": entrypoint,
                  "timestamp": ts, "message": {"role": "user", "content": prompt}}),
        ] + [dump(l) if isinstance(l, dict) else l for l in lines]
        text = "\n".join(body) + ("\n" if trailing_newline else "")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def register(self, pid, sid, cwd=None, status="idle", entrypoint="cli", name=None):
        folder = os.path.join(self.claude, "sessions")
        os.makedirs(folder, exist_ok=True)
        entry = {"pid": pid, "sessionId": sid, "cwd": cwd or self.repo, "status": status,
                 "entrypoint": entrypoint, "startedAt": 1_790_000_000_000,
                 "updatedAt": 1_790_000_100_000, "kind": "interactive"}
        if name:
            entry.update(name=name, nameSource="user")
        with open(os.path.join(folder, "%d.json" % pid), "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        with open(os.path.join(folder, "%d.deadbeef.key" % pid), "w", encoding="utf-8") as fh:
            fh.write(dump({"pid": 1, "sessionId": "from-key-file"}))

    # -- Codex fixtures
    UUID_A = "019a0000-0000-7000-8000-00000000000a"
    UUID_B = "019a0000-0000-7000-8000-00000000000b"
    UUID_SUB = "019a0000-0000-7000-8000-0000000000cc"

    def rollout(self, sid, cwd=None, originator="codex-tui", parent=None, segment=None,
                events=(), stamp="2026-09-20T10-00-00", prompt=PROMPT):
        folder = os.path.join(self.codex, "sessions", "2026", "09", "20")
        os.makedirs(folder, exist_ok=True)
        name = "rollout-%s-%s%s.jsonl" % (stamp, sid, "_" + segment if segment else "")
        path = os.path.join(folder, name)
        meta = {"id": sid, "cwd": cwd or self.repo, "originator": originator,
                "timestamp": "2026-09-20T10:00:00.000000Z", "source": "cli",
                "base_instructions": {"text": "x" * 2000}}
        if parent:
            meta.update(parent_thread_id=parent, source={"subagent": {}},
                        thread_source="subagent")
        lines = [{"timestamp": "2026-09-20T10:00:00Z", "type": "session_meta", "payload": meta},
                 {"type": "response_item", "payload": {
                     "type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
                 {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}}]
        lines += list(events)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(dump(l) for l in lines) + "\n")
        return path

    def index(self, *entries):
        os.makedirs(self.codex, exist_ok=True)
        with open(os.path.join(self.codex, "session_index.jsonl"), "a", encoding="utf-8") as fh:
            for sid, name in entries:
                fh.write(dump({"id": sid, "thread_name": name,
                               "updated_at": "2026-09-20T10:00:00.000000Z"}) + "\n")


class TestHelpers(unittest.TestCase):
    def test_parse_iso_handles_z_and_fraction_lengths(self):
        a = S.parse_iso("2026-09-20T10:00:00.123Z")
        b = S.parse_iso("2026-09-20T10:00:00.123000+00:00")
        self.assertAlmostEqual(a, b)
        self.assertIsNotNone(S.parse_iso("2026-09-20T10:00:00.1234567Z"))
        self.assertIsNone(S.parse_iso("yesterday"))
        self.assertIsNone(S.parse_iso(None))

    def test_surfaces(self):
        self.assertEqual(S.claude_surface("cli"), "cli")
        self.assertEqual(S.claude_surface("claude-desktop"), "desktop")
        self.assertEqual(S.claude_surface("sdk-cli"), "sdk")
        self.assertEqual(S.claude_surface("claude-vscode"), "ide")
        self.assertEqual(S.claude_surface(None), "unknown")
        self.assertEqual(S.codex_surface("codex-tui"), "cli")
        self.assertEqual(S.codex_surface("codex_exec"), "cli")
        self.assertEqual(S.codex_surface("Codex Desktop"), "desktop")
        self.assertEqual(S.codex_surface("codex_work_desktop"), "desktop")
        self.assertEqual(S.codex_surface("codex_vscode"), "ide")
        self.assertEqual(S.codex_surface("mystery"), "unknown")

    def test_vague_titles(self):
        for t in (None, "", "hi", "Untitled", "new chat 3", "fix", "  test  "):
            self.assertTrue(S.is_vague(t), t)
        for t in ("Fix doctor crash", "Relay handoff for codex"):
            self.assertFalse(S.is_vague(t), t)

    def test_derive_title_takes_eight_words_one_line(self):
        self.assertEqual(S.derive_title(PROMPT), DERIVED)
        self.assertEqual(S.derive_title("short\n\n**ask** `x`"), "short ask x")
        self.assertIsNone(S.derive_title("   "))

    def test_age_and_marks(self):
        self.assertEqual(S.age(100, now=130), "30s")
        self.assertEqual(S.age(0, now=7200), "2h")
        self.assertEqual(S.age(None), "–")
        self.assertEqual([S.rc_mark(v) for v in (True, False, None)],
                         ["✓", "✗", "–"])

    def test_pid_alive(self):
        self.assertTrue(S.pid_alive(os.getpid()))
        self.assertFalse(S.pid_alive(dead_pid()))
        self.assertFalse(S.pid_alive("nope"))
        self.assertFalse(S.pid_alive(0))

    @unittest.skipUnless(os.name == "posix", "POSIX signal-0 probe")
    def test_pid_alive_posix_probes_with_signal_zero_only(self):
        with mock.patch.object(S.os, "kill") as kill, \
                mock.patch.object(S, "_pid_alive_nt") as nt:
            self.assertTrue(S.pid_alive(4242))
        kill.assert_called_once_with(4242, 0)
        nt.assert_not_called()
        with mock.patch.object(S.os, "kill", side_effect=PermissionError):
            self.assertTrue(S.pid_alive(4242))
        with mock.patch.object(S.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(S.pid_alive(4242))

    def test_pid_alive_on_windows_never_signals(self):
        with mock.patch.object(S.os, "name", "nt"), \
                mock.patch.object(S.os, "kill") as kill, \
                mock.patch.object(S, "_pid_alive_nt", return_value=True) as nt:
            self.assertTrue(S.pid_alive("4242"))
            self.assertFalse(S.pid_alive(0))
        nt.assert_called_once_with(4242)
        kill.assert_not_called()

    def test_pid_alive_windows_tasklist_fallback(self):
        def fake_run(argv, **_kw):
            self.assertEqual(argv[:3], ["tasklist", "/FI", "PID eq 4242"])
            return mock.Mock(stdout=b'"python.exe","4242","Console","1","9,000 K"\r\n')
        with mock.patch.object(S.subprocess, "run", side_effect=fake_run):
            self.assertTrue(S._pid_alive_tasklist(4242))
        info = b"INFO: No tasks are running which match the specified criteria.\r\n"
        with mock.patch.object(S.subprocess, "run", return_value=mock.Mock(stdout=info)):
            self.assertFalse(S._pid_alive_tasklist(4242))
        with mock.patch.object(S.subprocess, "run", side_effect=OSError):
            self.assertFalse(S._pid_alive_tasklist(4242))

    @unittest.skipIf(os.name == "nt", "exercises the no-kernel32 path")
    def test_pid_alive_nt_without_kernel32_uses_tasklist(self):
        # ctypes has no WinDLL off Windows: the NT probe must degrade, not raise.
        with mock.patch.object(S, "_pid_alive_tasklist", return_value=True) as tl:
            self.assertTrue(S._pid_alive_nt(4242))
        tl.assert_called_once_with(4242)

    def test_homes_follow_env(self):
        with mock.patch.dict(os.environ, {"LASTCALL_CLAUDE_HOME": "/x/c",
                                          "LASTCALL_CODEX_HOME": "/x/o"}):
            self.assertEqual(S.claude_home(), "/x/c")
            self.assertEqual(S.codex_home(), "/x/o")
            self.assertEqual(S.claude_home("/y"), "/y")


class TestProjectRoot(Homes):
    def test_repo_worktree_and_plain_dir(self):
        self.assertEqual(S.project_root(os.path.join(self.repo, "a", "b")), self.repo)
        self.assertEqual(S.project_root(self.worktree), self.repo)
        self.assertEqual(S.project_root(self.plain), self.plain)
        self.assertIsNone(S.project_root(None))

    def test_deleted_claude_worktree_falls_back_to_its_project(self):
        gone = os.path.join(self.tmp, "nogit", "app", ".claude", "worktrees", "old")
        self.assertEqual(S.project_root(gone), os.path.join(self.tmp, "nogit", "app"))

    def test_label(self):
        self.assertEqual(S.project_label(self.repo), "widget")
        self.assertEqual(S.project_label(os.path.expanduser("~")), "~")
        self.assertEqual(S.project_label(None), "?")


class TestClaudeScan(Homes):
    def test_latest_custom_title_beats_ai_title(self):
        path = self.transcript("s1", lines=[
            {"type": "custom-title", "customTitle": "first", "sessionId": "s1"},
            {"type": "ai-title", "aiTitle": "ai later", "sessionId": "s1"},
            {"type": "custom-title", "customTitle": "second", "sessionId": "s1"},
            {"type": "ai-title", "aiTitle": "ai last", "sessionId": "s1"},
        ])
        rec = S.scan_claude_transcript(path)
        self.assertEqual((rec.title, rec.title_source), ("second", "custom"))
        self.assertEqual(rec.cwd, self.repo)
        self.assertEqual(rec.project, self.repo)
        self.assertEqual(rec.surface, "cli")
        self.assertAlmostEqual(rec.started_at, S.parse_iso("2026-09-20T10:00:00.123Z"))
        self.assertFalse(rec.remote_control)

    def test_ai_title_when_no_custom(self):
        path = self.transcript("s2", lines=[
            {"type": "ai-title", "aiTitle": "old", "sessionId": "s2"},
            {"type": "ai-title", "aiTitle": "new", "sessionId": "s2"}])
        rec = S.scan_claude_transcript(path)
        self.assertEqual((rec.title, rec.title_source), ("new", "ai"))

    def test_marker_inside_message_text_is_not_a_title(self):
        decoy = 'she typed "type":"custom-title" and "type":"bridge-session" here'
        path = self.transcript("s3", lines=[
            {"type": "assistant", "sessionId": "s3", "message": {"content": decoy}}])
        rec = S.scan_claude_transcript(path)
        self.assertIsNone(rec.title)
        self.assertEqual(rec.title_source, "none")
        self.assertFalse(rec.remote_control)
        self.assertIsNone(S.remote_control_connected(path))

    def test_nested_object_with_title_type_is_ignored(self):
        path = self.transcript("s4", lines=[
            {"type": "user", "sessionId": "s4",
             "toolUseResult": {"type": "custom-title", "customTitle": "nested"}}])
        self.assertIsNone(S.scan_claude_transcript(path).title)

    def test_remote_control_from_bridge_session(self):
        path = self.transcript("s5", lines=[
            {"type": "bridge-session", "sessionId": "s5", "bridgeSessionId": "cse_old",
             "lastSequenceNum": 1},
            {"type": "bridge-session", "sessionId": "s5", "bridgeSessionId": "cse_new",
             "lastSequenceNum": 9}])
        self.assertEqual(S.remote_control_connected(path), "cse_new")
        rec = S.scan_claude_transcript(path)
        self.assertTrue(rec.remote_control)
        self.assertEqual(rec.bridge_session_id, "cse_new")
        self.assertIsNone(S.remote_control_connected(None))

    def test_empty_and_garbage_transcripts(self):
        path = self.transcript("s6")
        open(path, "w", encoding="utf-8").close()
        rec = S.scan_claude_transcript(path)
        self.assertEqual(rec.session_id, "s6")
        self.assertIsNone(rec.title)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"type":"custom-title", broken\nnot json\n')
        self.assertIsNone(S.scan_claude_transcript(path).title)

    def test_surface_from_transcript_entrypoint(self):
        path = self.transcript("s7", entrypoint="claude-desktop")
        self.assertEqual(S.scan_claude_transcript(path).surface, "desktop")

    def test_subagent_transcripts_are_not_sessions(self):
        self.transcript("main")
        sub = os.path.join(self.claude, "projects", "-proj", "main", "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-1.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(dump({"type": "user", "cwd": self.repo}) + "\n")
        self.assertEqual([r.session_id for r in S.claude_sessions(self.claude)], ["main"])

    def test_first_prompt_skips_meta_and_command_lines(self):
        path = self.transcript("s8", lines=[
            {"type": "user", "sessionId": "s8", "message": {"content": "later prompt"}}])
        self.assertEqual(S.claude_first_prompt(path), PROMPT)

    def test_first_prompt_from_text_blocks_and_tool_results_skipped(self):
        folder = os.path.join(self.claude, "projects", "-p")
        os.makedirs(folder)
        path = os.path.join(folder, "s9.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dump({"type": "user", "toolUseResult": {}, "message": {
                "content": [{"type": "text", "text": "tool noise"}]}}) + "\n")
            fh.write(dump({"type": "user", "message": {
                "content": [{"type": "image"}, {"type": "text", "text": "block prompt"}]}}) + "\n")
        self.assertEqual(S.claude_first_prompt(path), "block prompt")

    def test_large_transcript_scans_fast(self):
        filler = dump({"type": "assistant", "message": {"content": "y" * 4000}})
        path = self.transcript("big", lines=[
            {"type": "custom-title", "customTitle": "early", "sessionId": "big"}]
            + [filler] * 5000)
        start = time.time()
        rec = S.scan_claude_transcript(path)
        self.assertEqual(rec.title, "early")
        self.assertLess(time.time() - start, 1.0)


class TestClaudeLive(Homes):
    def test_registry_ignores_key_files_and_dead_pids(self):
        self.transcript("alive", lines=[
            {"type": "bridge-session", "sessionId": "alive", "bridgeSessionId": "cse_1"}])
        self.transcript("gone")
        self.register(os.getpid(), "alive", status="busy", entrypoint="claude-desktop")
        self.register(dead_pid(), "gone")
        reg = S.claude_registry(self.claude)
        self.assertEqual(len(reg), 2)
        self.assertNotIn("from-key-file", [r["sessionId"] for r in reg])
        live = S.claude_live(self.claude)
        self.assertEqual([r.session_id for r in live], ["alive"])
        rec = live[0]
        self.assertTrue(rec.live)
        self.assertEqual(rec.pid, os.getpid())
        self.assertEqual(rec.status, "busy")
        self.assertEqual(rec.surface, "desktop")
        self.assertTrue(rec.remote_control)

    def test_live_without_transcript_uses_registry_name(self):
        self.register(os.getpid(), "fresh", name="Named in registry")
        rec = S.claude_live(self.claude)[0]
        self.assertIsNone(rec.transcript_path)
        self.assertEqual((rec.title, rec.title_source), ("Named in registry", "custom"))
        self.assertEqual(rec.started_at, 1_790_000_000.0)
        self.assertIn("fresh", [r.session_id for r in S.claude_sessions(self.claude)])

    def test_claude_sessions_marks_live(self):
        self.transcript("a")
        self.transcript("b")
        self.register(os.getpid(), "b")
        live = {r.session_id: r.live for r in S.claude_sessions(self.claude)}
        self.assertEqual(live, {"a": False, "b": True})

    def test_missing_home_is_empty(self):
        missing = os.path.join(self.tmp, "none")
        self.assertEqual(S.claude_sessions(missing), [])
        self.assertEqual(S.claude_live(missing), [])


class TestCodex(Homes):
    def test_index_latest_wins_and_subagents_skipped(self):
        self.rollout(self.UUID_A)
        self.rollout(self.UUID_B, cwd=self.worktree, originator="Codex Desktop")
        self.rollout(self.UUID_SUB, parent=self.UUID_A)
        self.index((self.UUID_A, "old name"), (self.UUID_A, "new name"))
        recs = {r.session_id: r for r in S.codex_sessions(self.codex, open_files={})}
        self.assertEqual(set(recs), {self.UUID_A, self.UUID_B})
        a, b = recs[self.UUID_A], recs[self.UUID_B]
        self.assertEqual((a.title, a.title_source), ("new name", "codex-index"))
        self.assertEqual((b.title, b.title_source), (None, "none"))
        self.assertEqual(a.surface, "cli")
        self.assertEqual(b.surface, "desktop")
        self.assertEqual(b.project, self.repo)
        self.assertIsNone(a.remote_control)
        self.assertFalse(a.live)

    def test_segments_group_under_one_session(self):
        first = self.rollout(self.UUID_A)
        seg = self.rollout(self.UUID_A, segment="019a0000-0000-7000-8000-0000000000ff",
                           stamp="2026-09-20T11-00-00")
        os.utime(first, (time.time() - 100, time.time() - 100))
        recs = S.codex_sessions(self.codex, open_files={})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].transcript_path, seg)

    def test_live_from_open_files_with_turn_status(self):
        busy = self.rollout(self.UUID_A, events=[
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "event_msg", "payload": {"type": "task_started"}}])
        self.rollout(self.UUID_B, events=[
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}}])
        sub = self.rollout(self.UUID_SUB, parent=self.UUID_A)
        open_files = {os.path.realpath(busy): 4242, os.path.realpath(sub): 4242}
        live = S.codex_live(self.codex, open_files=open_files)
        self.assertEqual([(r.session_id, r.status, r.pid) for r in live],
                         [(self.UUID_A, "busy", 4242)])

    def test_live_fallback_is_recent_rollout_while_codex_runs(self):
        old = self.rollout(self.UUID_A)
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.rollout(self.UUID_B)
        with mock.patch.object(S, "codex_running", return_value=True):
            live = S.codex_live(self.codex, open_files=None)
        self.assertEqual([r.session_id for r in live], [self.UUID_B])
        self.assertEqual(live[0].status, "idle")
        self.assertEqual(S.codex_live(self.codex, open_files=None), [])

    def test_first_prompt_prefers_user_message_event(self):
        path = self.rollout(self.UUID_A)
        self.assertEqual(S.codex_first_prompt(path), PROMPT)

    def test_first_prompt_from_response_item(self):
        folder = os.path.join(self.codex, "x")
        os.makedirs(folder)
        path = os.path.join(folder, "r.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for text in ("# AGENTS.md instructions", "real ask here"):
                fh.write(dump({"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]}}) + "\n")
        self.assertEqual(S.codex_first_prompt(path), "real ask here")

    def test_lsof_parser(self):
        out = "p77\nfcwd\nn/tmp\nn/x/sessions/rollout-a.jsonl\np88\nn/y/rollout-b.jsonl\n"
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(S.subprocess, "run", return_value=done):
            got = _REAL_OPEN_ROLLOUTS()
        self.assertEqual(got, {os.path.realpath("/x/sessions/rollout-a.jsonl"): 77,
                               os.path.realpath("/y/rollout-b.jsonl"): 88})
        with mock.patch.object(S.subprocess, "run", side_effect=OSError):
            self.assertIsNone(_REAL_OPEN_ROLLOUTS())


class TestStatus(Homes):
    def test_live_sessions_and_table(self):
        self.transcript("c1", lines=[
            {"type": "custom-title", "customTitle": "Relay work", "sessionId": "c1"},
            {"type": "bridge-session", "sessionId": "c1", "bridgeSessionId": "cse_9"}])
        self.transcript("c2")
        self.register(os.getpid(), "c1", status="busy")
        path = self.rollout(self.UUID_A)
        self.index((self.UUID_A, "Codex thing"))
        recs = S.live_sessions(self.claude, self.codex,
                               codex_open_files={os.path.realpath(path): 1})
        self.assertEqual(sorted(r.agent for r in recs), ["claude", "codex"])
        table = S.render_status(recs)
        self.assertIn("Relay work", table)
        self.assertIn("Codex thing", table)
        self.assertIn("✓", table)   # claude remote control
        self.assertIn("–", table)   # codex remote control n/a, no usage provider
        self.assertIn("SURFACE", table)
        self.assertNotIn("c2", [r.session_id for r in recs])
        self.assertEqual(S.render_status([]), "No live sessions.")

    def test_usage_provider_hook(self):
        rec = S.SessionRecord(agent="claude", session_id="x")

        class U:
            tokens, window = 90_000, 200_000
        with mock.patch.dict(S.USAGE_PROVIDERS, {}, clear=True):
            self.assertEqual(S._usage_cell(rec), "–")
            S.register_usage_provider("claude", lambda r: U())
            self.assertEqual(S._usage_cell(rec), "90k/200k 45%")
            S.register_usage_provider("claude", lambda r: 1 / 0)
            self.assertEqual(S._usage_cell(rec), "?")

    def test_status_dict_adds_the_context_fields(self):
        rec = S.SessionRecord(agent="claude", session_id="x")

        class U:
            tokens, window, window_source = 90_000, 200_000, "transcript"

        class NoWindow:
            tokens, window, window_source = 90_000, None, "unknown"
        keys = ("tokens", "window", "percent", "window_source")
        with mock.patch.dict(S.USAGE_PROVIDERS, {}, clear=True):
            d = S.status_dict(rec)
            self.assertEqual([d[k] for k in keys], [None] * 4)
            self.assertEqual(d["session_id"], "x")
            S.register_usage_provider("claude", lambda r: U())
            self.assertEqual([S.status_dict(rec)[k] for k in keys],
                             [90_000, 200_000, 45.0, "transcript"])
            S.register_usage_provider("claude", lambda r: NoWindow())
            self.assertEqual([S.status_dict(rec)[k] for k in keys], [90_000, None, None, None])
            S.register_usage_provider("claude", lambda r: None)
            self.assertEqual([S.status_dict(rec)[k] for k in keys], [None] * 4)
            S.register_usage_provider("claude", lambda r: 1 / 0)
            self.assertEqual([S.status_dict(rec)[k] for k in keys], [None] * 4)
        json.dumps(S.status_dict(rec))

    def test_to_dict(self):
        rec = S.SessionRecord(agent="codex", session_id="x", project=self.repo)
        d = rec.to_dict()
        self.assertEqual(d["project_name"], "widget")
        self.assertIn("surface", d)
        self.assertIsNone(d["handoff_chain"])


class TestPlan(Homes):
    def build(self, **kw):
        recs = S.all_sessions(self.claude, self.codex, codex_open_files={})
        return S.build_plan(recs, self.claude, self.codex, **kw)

    def items(self, plan):
        return {it["session_id"]: it for it in plan["items"]}

    def test_proposals_flags_and_actions(self):
        self.transcript("named", lines=[
            {"type": "ai-title", "aiTitle": "Fix doctor crash", "sessionId": "named"}])
        self.transcript("untitled", cwd=self.worktree)
        self.transcript("vague", prompt="Tighten the relay handshake timeout handling", lines=[
            {"type": "custom-title", "customTitle": "hi", "sessionId": "vague"}])
        self.transcript("tidy", lines=[
            {"type": "custom-title", "customTitle": "widget · Relay work",
             "sessionId": "tidy"}])
        self.transcript("desk", entrypoint="claude-desktop", lines=[
            {"type": "custom-title", "customTitle": "Desktop chat", "sessionId": "desk"}])
        self.transcript("live", lines=[
            {"type": "custom-title", "customTitle": "Running now", "sessionId": "live"}])
        self.register(os.getpid(), "live")
        self.rollout(self.UUID_A, cwd=self.plain)
        self.index((self.UUID_A, "Codex task"))
        items = self.items(self.build())

        self.assertEqual(items["named"]["proposed_title"], "widget · Fix doctor crash")
        self.assertEqual(items["named"]["action"], "rename")
        self.assertIn("unprefixed", items["named"]["flags"])

        u = items["untitled"]
        self.assertEqual(u["project"], "widget")  # worktree grouped with its repo
        self.assertIsNone(u["proposed_title"])
        self.assertEqual(u["derived"]["prefix"], "widget · ")
        self.assertEqual(u["_display"], "widget · " + DERIVED)
        self.assertIn("untitled", u["flags"])

        self.assertIn("vague", items["vague"]["flags"])
        self.assertEqual(items["vague"]["_display"],
                         "widget \u00b7 Tighten the relay handshake timeout handling")

        self.assertEqual(items["tidy"]["action"], "keep")
        self.assertEqual(items["desk"]["action"], "skip")
        self.assertIn("desktop", items["desk"]["flags"])
        self.assertEqual(items["live"]["action"], "skip")
        self.assertEqual(items["live"]["reason"], "live session")
        self.assertEqual(items[self.UUID_A]["proposed_title"], "scratch · Codex task")

        desk = self.items(self.build(include_desktop=True))["desk"]
        self.assertEqual(desk["action"], "rename")

    def test_duplicates_get_dates_then_counters(self):
        for sid, ts in (("d1", "2026-09-01T10:00:00Z"), ("d2", "2026-09-02T10:00:00Z"),
                        ("d3", "2026-09-02T11:00:00Z")):
            self.transcript(sid, ts=ts, lines=[
                {"type": "ai-title", "aiTitle": "Same thing", "sessionId": sid}])
        items = self.items(self.build())
        self.assertEqual(items["d1"]["proposed_title"], "widget · Same thing · 2026-09-01")
        self.assertEqual(items["d2"]["proposed_title"], "widget · Same thing · 2026-09-02")
        self.assertEqual(items["d3"]["proposed_title"],
                         "widget · Same thing · 2026-09-02 (2)")
        self.assertTrue(all("duplicate" in items[s]["flags"] for s in ("d1", "d2", "d3")))

    def test_no_prompt_falls_back_to_date(self):
        self.transcript("np", prompt="<system-reminder>only</system-reminder>")
        it = self.items(self.build())["np"]
        self.assertEqual(it["proposed_title"], "widget · session 2026-09-20")

    def test_plan_json_never_contains_prompt_text(self):
        self.transcript("untitled")
        self.rollout(self.UUID_A)
        plan = self.build()
        text = S.plan_to_json(plan)
        self.assertNotIn("refactor", text)
        self.assertNotIn("_display", text)
        self.assertEqual(json.loads(text)[S.PLAN_MARKER], S.PLAN_VERSION)
        self.assertIn("refactor", S.render_plan(plan))  # terminal only

    def test_render_plan_groups(self):
        self.transcript("a", lines=[{"type": "ai-title", "aiTitle": "Alpha job"}])
        self.rollout(self.UUID_A, cwd=self.plain, originator="Codex Desktop")
        plan = self.build()
        by_project = S.render_plan(plan)
        self.assertIn("[widget]", by_project)
        self.assertIn("[scratch]", by_project)
        by_surface = S.render_plan(plan, group_by="surface")
        self.assertIn("[cli]", by_surface)
        self.assertIn("[desktop]", by_surface)
        self.assertEqual(S.render_plan({"items": []}), "Nothing to tidy.")


class TestApply(Homes):
    def make_plan(self, **kw):
        recs = S.all_sessions(self.claude, self.codex, codex_open_files={})
        plan = S.build_plan(recs, self.claude, self.codex, **kw)
        path = os.path.join(self.tmp, "plan.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(S.plan_to_json(plan))
        return path, plan

    def apply(self, path, **kw):
        kw.setdefault("codex_open_files", {})
        return S.apply_plan(path, self.claude, self.codex, **kw)

    def test_apply_renames_claude_and_codex_with_backups(self):
        named = self.transcript("named", lines=[
            {"type": "ai-title", "aiTitle": "Fix doctor crash", "sessionId": "named"}],
            trailing_newline=False)
        untitled = self.transcript("untitled")
        self.rollout(self.UUID_A)
        self.index((self.UUID_A, "Codex task"))
        before = read(named)
        path, _ = self.make_plan()

        result = self.apply(path)
        self.assertEqual(len(result["applied"]), 3)
        self.assertEqual(result["skipped"], [])
        rec = S.scan_claude_transcript(named)
        self.assertEqual((rec.title, rec.title_source), ("widget · Fix doctor crash", "custom"))
        with open(named, encoding="utf-8") as fh:
            for line in fh:
                json.loads(line)  # missing trailing newline was repaired, not glued
        self.assertEqual(S.scan_claude_transcript(untitled).title, "widget · " + DERIVED)
        self.assertEqual(S.codex_index(self.codex)[self.UUID_A]["thread_name"],
                         "widget · Codex task")
        backups = result["backups"]
        self.assertEqual(len(backups), 3)
        for b in backups:
            self.assertTrue(b.startswith(os.path.join(self.tmp, "claude", "lastcall-backups"))
                            or b.startswith(os.path.join(self.tmp, "codex", "lastcall-backups")))
        named_backup = [b for b in backups if b.endswith("named.jsonl")][0]
        self.assertEqual(read(named_backup), before)

        again = self.apply(path)
        self.assertEqual(again["applied"], [])
        self.assertEqual({s["reason"] for s in again["skipped"]}, {"already has this title"})

    def test_dry_run_writes_nothing(self):
        named = self.transcript("named", lines=[
            {"type": "ai-title", "aiTitle": "Fix doctor crash", "sessionId": "named"}])
        before = read(named)
        path, _ = self.make_plan()
        result = self.apply(path, dry_run=True)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["backups"], [])
        self.assertEqual(read(named), before)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "lastcall-backups")))

    def test_session_that_went_live_is_skipped(self):
        named = self.transcript("named", lines=[
            {"type": "ai-title", "aiTitle": "Fix doctor crash", "sessionId": "named"}])
        before = read(named)
        path, _ = self.make_plan()
        self.register(os.getpid(), "named")
        result = self.apply(path)
        self.assertEqual(result["skipped"][0]["reason"], "live session")
        self.assertEqual(read(named), before)

    def test_live_codex_is_skipped(self):
        rollout = self.rollout(self.UUID_A)
        self.index((self.UUID_A, "Codex task"))
        path, _ = self.make_plan()
        result = self.apply(path, codex_open_files={os.path.realpath(rollout): 5})
        self.assertEqual(result["skipped"][0]["reason"], "live session")

    def test_user_edits_are_honoured(self):
        self.transcript("a", lines=[{"type": "ai-title", "aiTitle": "Alpha job"}])
        self.transcript("b", lines=[{"type": "ai-title", "aiTitle": "Beta job"}])
        self.transcript("c")
        path, plan = self.make_plan()
        data = load(path)
        for it in data["items"]:
            if it["session_id"] == "a":
                it["action"] = "skip"
            if it["session_id"] == "c":
                it["proposed_title"] = "My own name\nwith a newline"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        result = self.apply(path)
        self.assertEqual(sorted(a["session_id"] for a in result["applied"]), ["b", "c"])
        c = os.path.join(self.claude, "projects", "-proj", "c.jsonl")
        self.assertEqual(S.scan_claude_transcript(c).title, "My own name with a newline")

    def test_derived_title_must_still_match(self):
        untitled = self.transcript("untitled")
        path, _ = self.make_plan()
        self.transcript("untitled", prompt="An entirely different opening request for the agent")
        result = self.apply(path)
        self.assertEqual(result["skipped"][0]["reason"],
                         "transcript changed since the plan was made")
        self.assertIsNone(S.scan_claude_transcript(untitled).title)

    def test_refuses_other_homes_and_non_plans(self):
        self.transcript("a", lines=[{"type": "ai-title", "aiTitle": "Alpha job"}])
        path, _ = self.make_plan()
        with self.assertRaises(S.PlanError):
            S.apply_plan(path, os.path.join(self.tmp, "elsewhere"), self.codex,
                         codex_open_files={})
        bogus = os.path.join(self.tmp, "bogus.json")
        with open(bogus, "w", encoding="utf-8") as fh:
            json.dump({"items": []}, fh)
        with self.assertRaises(S.PlanError):
            self.apply(bogus)
        with self.assertRaises(S.PlanError):
            self.apply(os.path.join(self.tmp, "missing.json"))

    def test_refuses_paths_outside_home(self):
        self.transcript("a", lines=[{"type": "ai-title", "aiTitle": "Alpha job"}])
        path, _ = self.make_plan()
        outside = os.path.join(self.tmp, "a.jsonl")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        data = load(path)
        data["items"][0]["transcript_path"] = outside
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        result = self.apply(path)
        self.assertEqual(result["skipped"][0]["reason"],
                         "transcript missing or outside claude_home")
        self.assertEqual(read(outside, "r"), "{}\n")

    def test_desktop_sessions_stay_skipped_unless_planned_with_include_desktop(self):
        desk = self.transcript("desk", entrypoint="claude-desktop", lines=[
            {"type": "ai-title", "aiTitle": "Desktop chat"}])
        path, _ = self.make_plan()
        data = load(path)
        data["items"][0]["action"] = "rename"  # a hand edit cannot bypass the gate
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertEqual(self.apply(path)["skipped"][0]["reason"], "desktop session")
        path, _ = self.make_plan(include_desktop=True)
        self.assertEqual(len(self.apply(path)["applied"]), 1)
        self.assertEqual(S.scan_claude_transcript(desk).title, "widget · Desktop chat")


class TestCli(Homes):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli_sessions.main(list(argv) + ["--claude-home", self.claude,
                                                   "--codex-home", self.codex])
        return code, out.getvalue()

    def test_status_json_and_table(self):
        self.transcript("c1", lines=[{"type": "custom-title", "customTitle": "Relay work"}])
        self.register(os.getpid(), "c1", status="busy")
        code, out = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([(r["agent"], r["status"]) for r in rows], [("claude", "busy")])
        code, out = self.run_cli("status")
        self.assertIn("Relay work", out)
        code, out = self.run_cli("status", "--surface", "desktop")
        self.assertIn("No live sessions.", out)

    def test_tidy_plan_then_apply(self):
        named = self.transcript("named", lines=[
            {"type": "ai-title", "aiTitle": "Fix doctor crash", "sessionId": "named"}])
        before = read(named)
        code, out = self.run_cli("tidy")
        self.assertEqual(code, 0)
        self.assertIn("Read-only", out)
        self.assertEqual(read(named), before)

        plan = os.path.join(self.tmp, "out.json")
        code, out = self.run_cli("tidy", "--plan", plan)
        self.assertTrue(os.path.exists(plan))
        self.assertEqual(read(named), before)

        code, out = self.run_cli("tidy", "--apply", plan, "--dry-run")
        self.assertIn("would rename 1", out)
        self.assertEqual(read(named), before)

        code, out = self.run_cli("tidy", "--apply", plan)
        self.assertEqual(code, 0)
        self.assertIn("renamed 1", out)
        self.assertEqual(S.scan_claude_transcript(named).title, "widget · Fix doctor crash")

    def test_tidy_filters(self):
        self.transcript("a", lines=[{"type": "ai-title", "aiTitle": "Alpha job"}])
        self.rollout(self.UUID_A, cwd=self.plain)
        code, out = self.run_cli("tidy", "--json", "--agent", "codex")
        self.assertEqual([i["agent"] for i in json.loads(out)["items"]], ["codex"])
        code, out = self.run_cli("tidy", "--json", "--project", "scratch")
        self.assertEqual([i["project"] for i in json.loads(out)["items"]], ["scratch"])
        code, out = self.run_cli("tidy", "--json", "--older-than", "1")
        self.assertEqual(json.loads(out)["items"], [])

    def test_apply_errors_are_reported(self):
        code, _ = self.run_cli("tidy", "--apply", os.path.join(self.tmp, "nope.json"))
        self.assertEqual(code, 1)
        code, _ = self.run_cli("tidy", "--apply", "x", "--plan", "y")
        self.assertEqual(code, 2)

    def test_runs_as_module(self):
        env = dict(os.environ, PYTHONPATH=LIB, LASTCALL_CLAUDE_HOME=self.claude,
                   LASTCALL_CODEX_HOME=self.codex)
        proc = subprocess.run([sys.executable, "-m", "lastcall_core.cli_sessions", "tidy", "--json",
                               "--agent", "claude"], env=env, capture_output=True, text=True,
                              timeout=60, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["claude_home"], self.claude)


if __name__ == "__main__":
    unittest.main()
