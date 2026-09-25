#!/usr/bin/env python3
"""Tests for the agent adapter layer (plugins/lastcall/lib/lastcall_core/agents).

Every fixture here is synthetic: the structure mirrors what Claude Code and
Codex write, the content is invented.

The library is a package called ``lastcall``, and the hook script is a module
of the same name that the other test files import. Both cannot sit in
sys.modules at once, so the library is loaded in isolation and then taken back
out, leaving ``import lastcall`` meaning the script for everyone else.
"""

import ast
import importlib
import importlib.util
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "lastcall")
LIB = os.path.join(PLUGIN, "lib")
SCRIPT = os.path.join(PLUGIN, "scripts", "lastcall.py")


def _load_library():
    saved = {name: module for name, module in sys.modules.items()
             if name == "lastcall_core" or name.startswith("lastcall_core.")}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, LIB)
    try:
        return {
            "agents": importlib.import_module("lastcall_core.agents"),
            "base": importlib.import_module("lastcall_core.agents.base"),
            "claude": importlib.import_module("lastcall_core.agents.claude"),
            "codex": importlib.import_module("lastcall_core.agents.codex"),
            "tail": importlib.import_module("lastcall_core.tail"),
        }
    finally:
        sys.path.remove(LIB)
        for name in [n for n in sys.modules
                     if n == "lastcall_core" or n.startswith("lastcall_core.")]:
            del sys.modules[name]
        sys.modules.update(saved)


def _load_script():
    spec = importlib.util.spec_from_file_location("_lastcall_script_for_parity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIBRARY = _load_library()
agents = LIBRARY["agents"]
base = LIBRARY["base"]
claude_mod = LIBRARY["claude"]
codex_mod = LIBRARY["codex"]
tail = LIBRARY["tail"]

CLAUDE = agents.get_agent("claude")
CODEX = agents.get_agent("codex")

SID = "11111111-2222-4333-8444-555555555555"
OTHER_SID = "99999999-8888-4777-8666-555555555555"
SEGMENT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


# --------------------------------------------------------------------------
# Synthetic fixtures
# --------------------------------------------------------------------------

_ids = iter(range(1, 1000000))


def claude_assistant(tokens, model="claude-test-1", session=SID, sidechain=False,
                     output=500, stamp="2026-01-01T00:00:00.000Z", msg_id=None,
                     text=None, block="text"):
    content = [{"type": block, "text": text or "synthetic reply"}] if block == "text" \
        else [{"type": block}]
    return {
        "type": "assistant",
        "sessionId": session,
        "isSidechain": sidechain,
        "timestamp": stamp,
        "message": {
            "id": msg_id or "msg_synthetic_%d" % next(_ids),
            "role": "assistant",
            "content": content,
            "model": model,
            "usage": {
                "input_tokens": 10 if tokens else 0,
                "cache_read_input_tokens": max(0, tokens - 10),
                "cache_creation_input_tokens": 0,
                "output_tokens": output,
            },
        },
    }


def claude_synthetic(session=SID):
    return {
        "type": "assistant",
        "sessionId": session,
        "isSidechain": False,
        "message": {
            "model": "<synthetic>",
            "usage": {"input_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 0},
        },
    }


def claude_boundary(post_tokens=12345, session=SID, stamp="2026-01-01T01:00:00.000Z"):
    meta = {"trigger": "auto", "preTokens": 900000}
    if post_tokens is not None:
        meta["postTokens"] = post_tokens
    return {"type": "system", "subtype": "compact_boundary", "sessionId": session,
            "isSidechain": False, "timestamp": stamp, "compactMetadata": meta}


def claude_user(text="synthetic user turn", session=SID):
    return {"type": "user", "sessionId": session, "isSidechain": False,
            "message": {"role": "user", "content": text}}


def codex_line(kind, payload, ordinal=0, stamp="2026-01-01T00:00:00.000Z"):
    return {"timestamp": stamp, "ordinal": ordinal, "type": kind, "payload": payload}


def codex_token_count(last_total, window=258400, cumulative=None, stamp="2026-01-01T00:00:00.000Z"):
    cumulative = cumulative if cumulative is not None else last_total * 40
    return codex_line("event_msg", {
        "type": "token_count",
        "info": {
            "total_token_usage": {"input_tokens": cumulative, "cached_input_tokens": 0,
                                  "output_tokens": 0, "total_tokens": cumulative},
            "last_token_usage": {"input_tokens": last_total - 50, "cached_input_tokens": 0,
                                 "output_tokens": 50, "total_tokens": last_total},
            "model_context_window": window,
        },
        "rate_limits": {"primary": {"used_percent": 1.0}},
    }, stamp=stamp)


def codex_usage_record(total, response_id="resp_1", turn_id="turn-1",
                       stamp="2026-01-01T00:00:00.000Z"):
    usage = {"input_tokens": total - 40, "cached_input_tokens": 0,
             "output_tokens": 40, "total_tokens": total}
    return codex_line("token_usage_record", {
        "thread_id": SID, "turn_id": turn_id, "session_id": SID,
        "response_id": response_id, "usage": usage,
        "turn_token_usage": dict(usage, total_tokens=total * 3),
        "thread_token_usage": dict(usage, total_tokens=total * 50),
    }, stamp=stamp)


def codex_rate_limits_only():
    return codex_line("event_msg", {"type": "token_count", "info": None,
                                    "rate_limits": {"primary": {"used_percent": 2.0}}})


def codex_turn_context(model="gpt-test", turn_id="turn-1"):
    return codex_line("turn_context", {"turn_id": turn_id, "model": model,
                                       "cwd": "/tmp/synthetic", "effort": "medium"})


def codex_task_started(window=258400, turn_id="turn-1"):
    return codex_line("event_msg", {"type": "task_started", "turn_id": turn_id,
                                    "model_context_window": window})


def codex_compacted(padding=0):
    return codex_line("compacted", {"message": "", "replacement_history": ["x" * padding],
                                    "window_number": 1})


def codex_message(text="synthetic agent message"):
    return codex_line("response_item", {"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text", "text": text}]})


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lastcall-adapters-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_jsonl(self, path, entries, raw_suffix=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")
            handle.write(raw_suffix)
        return path

    def claude_transcript(self, entries, session=SID):
        path = os.path.join(self.tmp, "claude", "projects", "-tmp-synthetic", session + ".jsonl")
        return self.write_jsonl(path, entries)

    def codex_home(self):
        return os.path.join(self.tmp, "codex")

    def rollout(self, entries, session=SID, day=("2026", "01", "02"),
                stamp="2026-01-02T10-00-00", segment=None):
        name = "rollout-%s-%s%s.jsonl" % (stamp, session, "_" + segment if segment else "")
        path = os.path.join(self.codex_home(), "sessions", *day, name)
        return self.write_jsonl(path, entries)


# --------------------------------------------------------------------------
# Reverse reader
# --------------------------------------------------------------------------

class TestReverseReader(TempDirCase):
    def test_yields_newest_first_skipping_blanks_and_crlf(self):
        path = os.path.join(self.tmp, "f.jsonl")
        with open(path, "wb") as handle:
            handle.write(b"one\r\n\r\ntwo\r\n   \nthree\r\n")
        self.assertEqual(list(tail.iter_lines_reverse(path)), [b"three", b"two", b"one"])

    def test_reassembles_a_line_spanning_many_chunks(self):
        path = os.path.join(self.tmp, "f.jsonl")
        long_line = b"x" * 10000
        with open(path, "wb") as handle:
            handle.write(b"first\n" + long_line + b"\nlast")
        lines = list(tail.iter_lines_reverse(path, chunk=7))
        self.assertEqual(lines, [b"last", long_line, b"first"])

    def test_never_yields_a_line_cut_by_the_limit(self):
        path = os.path.join(self.tmp, "f.jsonl")
        with open(path, "wb") as handle:
            handle.write(b"a" * 100 + b"\n" + b"b" * 10 + b"\n")
        lines = list(tail.iter_lines_reverse(path, limit=40, chunk=16))
        self.assertEqual(lines, [b"b" * 10])

    def test_empty_file_yields_nothing(self):
        path = os.path.join(self.tmp, "f.jsonl")
        open(path, "wb").close()
        self.assertEqual(list(tail.iter_lines_reverse(path)), [])


# --------------------------------------------------------------------------
# Claude adapter
# --------------------------------------------------------------------------

class TestClaudeUsage(TempDirCase):
    def test_reads_the_newest_main_session_record(self):
        path = self.claude_transcript([claude_assistant(50000), claude_user(),
                                       claude_assistant(64000, stamp="2026-01-01T00:05:00Z")])
        usage = CLAUDE.read_usage(path, SID)
        self.assertEqual(usage.tokens, 64000)
        self.assertEqual(usage.agent, "claude")
        self.assertEqual(usage.model, "claude-test-1")
        self.assertEqual(usage.session_id, SID)
        self.assertEqual(usage.measured_at, "2026-01-01T00:05:00Z")
        self.assertFalse(usage.compacted)
        self.assertFalse(usage.stale)

    def test_skips_sidechains_and_other_sessions(self):
        path = self.claude_transcript([
            claude_assistant(70000),
            claude_assistant(5000, sidechain=True),
            claude_assistant(3000, session=OTHER_SID),
        ])
        self.assertEqual(CLAUDE.read_usage(path, SID).tokens, 70000)

    def test_synthetic_entries_do_not_read_as_an_empty_context(self):
        path = self.claude_transcript([claude_assistant(180000), claude_synthetic()])
        usage = CLAUDE.read_usage(path, SID)
        self.assertEqual(usage.tokens, 180000)

    def test_zero_usage_from_a_real_model_is_skipped_too(self):
        path = self.claude_transcript([claude_assistant(90000), claude_assistant(0)])
        self.assertEqual(CLAUDE.read_usage(path, SID).tokens, 90000)

    def test_only_synthetic_records_means_no_measurement(self):
        path = self.claude_transcript([claude_user(), claude_synthetic()])
        self.assertIsNone(CLAUDE.read_usage(path, SID))

    def test_output_tokens_only_when_asked(self):
        path = self.claude_transcript([claude_assistant(10000, output=700)])
        self.assertEqual(CLAUDE.read_usage(path, SID).tokens, 10000)
        self.assertEqual(CLAUDE.read_usage(path, SID, include_output=True).tokens, 10700)

    def test_missing_or_garbled_transcripts_are_not_errors(self):
        self.assertIsNone(CLAUDE.read_usage(None))
        self.assertIsNone(CLAUDE.read_usage(os.path.join(self.tmp, "absent.jsonl")))
        path = self.claude_transcript(["{not json", "[1, 2]", claude_assistant(42000), "garbage"])
        self.assertEqual(CLAUDE.read_usage(path, SID).tokens, 42000)

    def test_compaction_newer_than_every_record_uses_post_tokens(self):
        path = self.claude_transcript([claude_assistant(850000), claude_boundary(15000),
                                       claude_user("synthetic summary")])
        usage = CLAUDE.read_usage(path, SID)
        self.assertEqual(usage.tokens, 15000)
        self.assertTrue(usage.compacted)
        self.assertFalse(usage.stale)
        self.assertEqual(usage.measured_at, "2026-01-01T01:00:00.000Z")

    def test_compaction_without_post_tokens_is_flagged_stale(self):
        path = self.claude_transcript([claude_assistant(850000), claude_boundary(None)])
        usage = CLAUDE.read_usage(path, SID)
        self.assertEqual(usage.tokens, 850000)
        self.assertTrue(usage.compacted)
        self.assertTrue(usage.stale)

    def test_compacted_only_until_the_next_measurement(self):
        entries = [claude_assistant(850000), claude_boundary(15000), claude_synthetic(),
                   claude_assistant(60000)]
        usage = CLAUDE.read_usage(self.claude_transcript(entries), SID)
        self.assertEqual(usage.tokens, 60000)
        self.assertTrue(usage.compacted)
        entries.append(claude_assistant(61000))
        usage = CLAUDE.read_usage(self.claude_transcript(entries), SID)
        self.assertEqual(usage.tokens, 61000)
        self.assertFalse(usage.compacted)

    def test_one_compaction_has_one_identity_before_and_after_the_next_reply(self):
        """Review finding: the engine keyed compactions on record_id|measured_at,
        which differ between "boundary newer than every reply" and "boundary
        between the newest two replies" — one compaction re-armed twice."""
        entries = [claude_assistant(850000), claude_boundary(15000)]
        first = CLAUDE.read_usage(self.claude_transcript(entries), SID)
        entries.append(claude_assistant(60000))
        second = CLAUDE.read_usage(self.claude_transcript(entries), SID)
        self.assertTrue(first.compacted and second.compacted)
        self.assertNotEqual((first.record_id, first.measured_at),
                            (second.record_id, second.measured_at))
        self.assertIsNotNone(first.compaction_id)
        self.assertEqual(first.compaction_id, second.compaction_id)

    def test_window_is_proved_by_evidence_never_guessed_from_the_name(self):
        path = self.claude_transcript([claude_assistant(150000, model="claude-opus-test")])
        usage = CLAUDE.read_usage(path, SID)
        self.assertIsNone(usage.window)
        self.assertEqual(usage.window_source, "unknown")
        self.assertIsNone(usage.percent)
        path = self.claude_transcript([claude_assistant(350000)])
        usage = CLAUDE.read_usage(path, SID)
        self.assertEqual((usage.window, usage.window_source), (1_000_000, "evidence"))
        self.assertAlmostEqual(usage.percent, 35.0)
        self.assertEqual(usage.remaining, 650000)

    def test_explicit_one_million_marker_is_a_known_window(self):
        self.assertEqual(CLAUDE.known_window("claude-opus-test[1m]"), (1_000_000, "model-name"))
        self.assertEqual(CLAUDE.known_window("claude-opus-test"), (None, "unknown"))
        self.assertEqual(CLAUDE.known_window(None), (None, "unknown"))

    def test_ported_helpers_match_the_hook_script(self):
        script = _load_script()
        for usage in ({"input_tokens": 3, "cache_read_input_tokens": 690000,
                       "cache_creation_input_tokens": 1200, "output_tokens": 9},
                      {"input_tokens": None, "output_tokens": 4}, {}):
            for include in (False, True):
                self.assertEqual(claude_mod.count_tokens(usage, include),
                                 script.count_tokens(usage, include))
        for peak in (None, 0, 150000, 200000, 200001, 999999, 1000000, 1000001):
            self.assertEqual(claude_mod.window_from_evidence(peak),
                             script.window_from_evidence(peak))
        path = self.claude_transcript([claude_assistant(77000), claude_assistant(5, sidechain=True)])
        self.assertEqual(claude_mod.latest_usage(path, SID), script.latest_usage(path, SID))

    def test_latest_usage_keeps_its_old_shape(self):
        path = self.claude_transcript([claude_assistant(77000)])
        usage, model = claude_mod.latest_usage(path, SID)
        self.assertEqual(claude_mod.count_tokens(usage), 77000)
        self.assertEqual(model, "claude-test-1")
        self.assertEqual(claude_mod.latest_usage(self.claude_transcript([claude_user()]), SID),
                         (None, None))


    def test_blocks_of_one_response_are_not_a_previous_reading(self):
        # One API response is written as several entries sharing message.id.
        entries = [claude_assistant(850000), claude_boundary(15000),
                   claude_assistant(60000, msg_id="msg_a", block="thinking"),
                   claude_assistant(60000, msg_id="msg_a", text="done")]
        usage = CLAUDE.read_usage(self.claude_transcript(entries), SID)
        self.assertEqual(usage.tokens, 60000)
        self.assertTrue(usage.compacted)
        self.assertEqual(usage.record_id, "msg_a")


class TestClaudeFreshness(TempDirCase):
    def append_later(self, path, entry, delay=0.2):
        def append():
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        timer = threading.Timer(delay, append)
        timer.start()
        self.addCleanup(timer.cancel)
        return timer

    def test_waits_for_the_response_the_hook_fired_for(self):
        path = self.claude_transcript([claude_assistant(40000, msg_id="msg_old", text="earlier")])
        self.append_later(path, claude_assistant(45000, msg_id="msg_new", text="All done.\n"))
        started = time.monotonic()
        usage = CLAUDE.read_usage(path, SID, fresh_timeout=3.0,
                                  last_assistant_message="All done.")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual((usage.tokens, usage.record_id, usage.fresh), (45000, "msg_new", True))

    def test_gives_up_after_the_timeout_with_the_last_reading(self):
        path = self.claude_transcript([claude_assistant(40000, msg_id="msg_old", text="earlier")])
        started = time.monotonic()
        usage = CLAUDE.read_usage(path, SID, fresh_timeout=0.3,
                                  last_assistant_message="never written")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2.0)
        self.assertEqual((usage.tokens, usage.fresh), (40000, False))

    def test_a_new_message_id_is_enough_without_the_text(self):
        path = self.claude_transcript([claude_assistant(40000, msg_id="msg_old")])
        usage = CLAUDE.read_usage(path, SID, previous_record_id="msg_old")
        self.assertFalse(usage.fresh)
        self.append_later(path, claude_assistant(41000, msg_id="msg_new", block="tool_use"), 0.1)
        usage = CLAUDE.read_usage(path, SID, fresh_timeout=3.0, previous_record_id="msg_old")
        self.assertEqual((usage.tokens, usage.fresh), (41000, True))

    def test_text_split_over_entries_still_matches(self):
        path = self.claude_transcript([
            claude_assistant(40000, msg_id="msg_a", text="First part."),
            claude_assistant(40000, msg_id="msg_a", block="tool_use"),
            claude_assistant(40000, msg_id="msg_a", text="Second part."),
        ])
        usage = CLAUDE.read_usage(path, SID, last_assistant_message="First part.\nSecond part.")
        self.assertTrue(usage.fresh)
        usage = CLAUDE.read_usage(path, SID, last_assistant_message="Second part.")
        self.assertTrue(usage.fresh)

    def test_no_criterion_means_no_wait_and_no_claim(self):
        path = self.claude_transcript([claude_assistant(40000)])
        started = time.monotonic()
        usage = CLAUDE.read_usage(path, SID, fresh_timeout=5.0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsNone(usage.fresh)


class TestClaudeOutput(unittest.TestCase):
    def test_context_rides_in_hook_specific_output(self):
        output = CLAUDE.format_output("Stop", "warning text")
        self.assertEqual(output, {"suppressOutput": True, "hookSpecificOutput": {
            "hookEventName": "Stop", "additionalContext": "warning text"}})

    def test_block_only_on_stop(self):
        output = CLAUDE.format_output("Stop", "warning", "write the handoff")
        self.assertEqual((output["decision"], output["reason"]), ("block", "write the handoff"))
        output = CLAUDE.format_output("SessionStart", "hello", "ignored")
        self.assertNotIn("decision", output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_nothing_to_say_means_no_output(self):
        self.assertIsNone(CLAUDE.format_output("Stop"))
        self.assertIsNone(CLAUDE.format_output("PostCompact", None, "block ignored here"))

    def test_nothing_that_continues_the_model_once_stop_hook_is_active(self):
        # Stop's additionalContext re-invokes the model just as a block does;
        # with stop_hook_active set that loops until Claude's cap.
        output = CLAUDE.format_output("Stop", "warning", "write it", stop_hook_active=True)
        self.assertEqual(output, {"suppressOutput": True, "systemMessage": "warning"})
        self.assertIsNone(CLAUDE.format_output("Stop", None, "write it", stop_hook_active=True))
        output = CLAUDE.format_output("SessionStart", "hello", stop_hook_active=True)
        self.assertIn("hookSpecificOutput", output)

    def test_events_without_additional_context_get_a_system_message(self):
        output = CLAUDE.format_output("Notification", "note")
        self.assertEqual(output, {"suppressOutput": True, "systemMessage": "note"})


# --------------------------------------------------------------------------
# Codex adapter
# --------------------------------------------------------------------------

class TestCodexUsage(TempDirCase):
    def read(self, path, session_id=SID, **kwargs):
        kwargs.setdefault("env", {"CODEX_HOME": self.codex_home()})
        return CODEX.read_usage(path, session_id, **kwargs)

    def test_context_is_last_token_usage_not_the_cumulative_total(self):
        path = self.rollout([codex_task_started(), codex_turn_context(),
                             codex_token_count(40000, cumulative=9000000),
                             codex_message(),
                             codex_token_count(52000, cumulative=9100000,
                                               stamp="2026-01-02T10:01:00.000Z")])
        usage = self.read(path)
        self.assertEqual(usage.tokens, 52000)
        self.assertEqual((usage.window, usage.window_source), (258400, "transcript"))
        self.assertEqual(usage.agent, "codex")
        self.assertEqual(usage.model, "gpt-test")
        self.assertEqual(usage.turn_id, "turn-1")
        self.assertEqual(usage.measured_at, "2026-01-02T10:01:00.000Z")
        self.assertFalse(usage.compacted)
        self.assertFalse(usage.stale)
        self.assertAlmostEqual(usage.percent, 52000 * 100.0 / 258400)

    def test_a_newer_usage_record_beats_the_token_count(self):
        # PostToolUse: the response that called the tool has its record, but
        # its token_count is only written after the tool has run.
        path = self.rollout([codex_turn_context(), codex_usage_record(40000, "resp_a"),
                             codex_token_count(40000, window=200000),
                             codex_usage_record(47000, "resp_b", turn_id="turn-2",
                                                stamp="2026-01-02T10:02:00.000Z")])
        usage = self.read(path)
        self.assertEqual((usage.tokens, usage.record_id), (47000, "resp_b"))
        self.assertEqual((usage.window, usage.window_source), (200000, "transcript"))
        self.assertEqual(usage.measured_at, "2026-01-02T10:02:00.000Z")

    def test_a_token_count_takes_its_response_id_from_its_record(self):
        path = self.rollout([codex_turn_context(), codex_usage_record(40000, "resp_a"),
                             codex_token_count(40000), codex_usage_record(47000, "resp_b"),
                             codex_token_count(47000)])
        usage = self.read(path)
        self.assertEqual((usage.tokens, usage.record_id), (47000, "resp_b"))
        self.assertFalse(usage.compacted)

    def test_compaction_is_seen_through_record_and_count_pairs(self):
        entries = [codex_turn_context(), codex_usage_record(225000, "resp_a"),
                   codex_token_count(225000), codex_compacted(),
                   codex_turn_context(turn_id="turn-2"), codex_usage_record(17000, "resp_b")]
        usage = self.read(self.rollout(entries))
        self.assertEqual(usage.tokens, 17000)
        self.assertTrue(usage.compacted)
        entries.append(codex_token_count(17000))
        usage = self.read(self.rollout(entries))
        self.assertEqual((usage.tokens, usage.record_id), (17000, "resp_b"))
        self.assertTrue(usage.compacted)
        entries += [codex_usage_record(30000, "resp_c"), codex_token_count(30000)]
        self.assertFalse(self.read(self.rollout(entries)).compacted)

    def test_one_compaction_has_one_identity_stale_or_not(self):
        compacted = codex_compacted()
        compacted["timestamp"] = "2026-01-02T10:05:00.000Z"
        entries = [codex_turn_context(), codex_usage_record(225000, "resp_a"),
                   codex_token_count(225000), compacted]
        stale = self.read(self.rollout(entries))
        entries += [codex_usage_record(17000, "resp_b"), codex_token_count(17000)]
        fresh = self.read(self.rollout(entries))
        self.assertTrue(stale.stale and stale.compacted and fresh.compacted)
        self.assertEqual(stale.compaction_id, "timestamp:2026-01-02T10:05:00.000Z")
        self.assertEqual(fresh.compaction_id, stale.compaction_id)

    def test_rate_limit_only_updates_are_ignored(self):
        path = self.rollout([codex_turn_context(), codex_token_count(30000),
                             codex_rate_limits_only()])
        self.assertEqual(self.read(path).tokens, 30000)

    def test_window_falls_back_to_task_started(self):
        entry = codex_token_count(30000)
        del entry["payload"]["info"]["model_context_window"]
        path = self.rollout([codex_task_started(window=128000), codex_turn_context(), entry])
        usage = self.read(path)
        self.assertEqual((usage.window, usage.window_source), (128000, "transcript"))

    def test_window_falls_back_to_the_models_cache(self):
        os.makedirs(self.codex_home(), exist_ok=True)
        with open(os.path.join(self.codex_home(), "models_cache.json"), "w") as handle:
            json.dump({"models": [
                {"slug": "gpt-test", "context_window": 272000,
                 "effective_context_window_percent": 95},
                {"slug": "gpt-other", "context_window": 100000},
            ]}, handle)
        env = {"CODEX_HOME": self.codex_home()}
        self.assertEqual(CODEX.known_window("gpt-test", env), (258400, "models-cache"))
        self.assertEqual(CODEX.known_window("gpt-other", env), (95000, "models-cache"))
        self.assertEqual(CODEX.known_window("gpt-unknown", env), (None, "unknown"))
        entry = codex_token_count(30000)
        del entry["payload"]["info"]["model_context_window"]
        usage = self.read(self.rollout([codex_turn_context(), entry]))
        self.assertEqual((usage.window, usage.window_source), (258400, "models-cache"))

    def test_no_window_anywhere_is_unknown_not_a_guess(self):
        entry = codex_token_count(30000)
        del entry["payload"]["info"]["model_context_window"]
        usage = self.read(self.rollout([entry]))
        self.assertIsNone(usage.window)
        self.assertEqual(usage.window_source, "unknown")

    def test_model_hint_is_used_when_no_turn_context_is_found(self):
        usage = self.read(self.rollout([codex_token_count(30000)]), model="gpt-hint")
        self.assertEqual(usage.model, "gpt-hint")
        usage = self.read(self.rollout([codex_turn_context(model="gpt-newer", turn_id="turn-9"),
                                        codex_token_count(30000)]))
        self.assertEqual((usage.model, usage.turn_id), ("gpt-newer", "turn-9"))

    def test_compaction_is_reported_once_then_clears(self):
        entries = [codex_turn_context(), codex_token_count(225000), codex_compacted(),
                   codex_turn_context(turn_id="turn-2"), codex_token_count(17000)]
        usage = self.read(self.rollout(entries))
        self.assertEqual(usage.tokens, 17000)
        self.assertTrue(usage.compacted)
        self.assertFalse(usage.stale)
        entries.append(codex_token_count(33000))
        usage = self.read(self.rollout(entries))
        self.assertEqual(usage.tokens, 33000)
        self.assertFalse(usage.compacted)

    def test_compaction_newer_than_every_token_count_is_stale(self):
        usage = self.read(self.rollout([codex_turn_context(), codex_token_count(225000),
                                        codex_compacted()]))
        self.assertEqual(usage.tokens, 225000)
        self.assertTrue(usage.compacted)
        self.assertTrue(usage.stale)

    def test_a_compaction_bigger_than_a_read_chunk_is_stepped_over(self):
        entries = [codex_turn_context(), codex_token_count(225000),
                   codex_compacted(padding=3 * tail.TAIL_CHUNK_BYTES),
                   codex_token_count(18000)]
        usage = self.read(self.rollout(entries))
        self.assertEqual(usage.tokens, 18000)
        self.assertTrue(usage.compacted)

    def test_lines_with_unexpected_key_order_are_still_understood(self):
        reordered = '{"payload": {"info": {"last_token_usage": {"total_tokens": 61000}, ' \
                    '"model_context_window": 258400}, "type": "token_count"}, ' \
                    '"type": "event_msg", "timestamp": "2026-01-02T00:00:00Z"}'
        usage = self.read(self.rollout([codex_turn_context(), codex_token_count(40000), reordered]))
        self.assertEqual(usage.tokens, 61000)

    def test_segmented_rollout_falls_back_to_a_sibling_with_usage(self):
        older = self.rollout([codex_turn_context(), codex_token_count(88000)],
                             day=("2026", "01", "01"), stamp="2026-01-01T09-00-00")
        segment = self.rollout([codex_line("session_meta", {"id": SID})],
                               day=("2026", "01", "03"), stamp="2026-01-03T09-00-00",
                               segment=SEGMENT)
        now = time.time()
        os.utime(older, (now - 100, now - 100))
        os.utime(segment, (now, now))
        usage = self.read(segment, session_id=None)
        self.assertEqual(usage.tokens, 88000)
        self.assertEqual(usage.session_id, SID)

    def test_newest_sibling_wins(self):
        first = self.rollout([codex_token_count(10000)], stamp="2026-01-02T08-00-00")
        second = self.rollout([codex_token_count(20000)], stamp="2026-01-02T09-00-00",
                              segment=SEGMENT)
        now = time.time()
        os.utime(first, (now - 100, now - 100))
        os.utime(second, (now, now))
        self.assertEqual(self.read(None).tokens, 20000)

    def test_found_by_session_id_when_no_path_is_given(self):
        self.rollout([codex_token_count(12000)])
        self.assertEqual(self.read(None).tokens, 12000)
        self.assertIsNone(self.read(None, session_id=OTHER_SID))

    def test_never_globs_on_something_that_is_not_a_session_id(self):
        self.rollout([codex_token_count(12000)])
        self.assertEqual(CODEX.rollouts_for("*", env={"CODEX_HOME": self.codex_home()}), [])
        self.assertIsNone(self.read(None, session_id="*"))

    def test_session_id_is_read_from_the_file_name(self):
        name = "rollout-2026-01-02T10-00-00-%s_%s.jsonl" % (SID, SEGMENT)
        self.assertEqual(codex_mod.session_id_from_path("/x/" + name), SID)
        self.assertIsNone(codex_mod.session_id_from_path("/x/%s.jsonl" % SID))
        self.assertIsNone(codex_mod.session_id_from_path(None))


class TestCodexOutput(unittest.TestCase):
    STOP_KEYS = {"continue", "stopReason", "suppressOutput", "systemMessage",
                 "decision", "reason"}

    def test_stop_output_never_carries_hook_specific_output(self):
        for message, reason in (("warn", None), (None, "block"), ("warn", "block")):
            output = CODEX.format_output("Stop", message, reason)
            self.assertNotIn("hookSpecificOutput", output)
            self.assertLessEqual(set(output), self.STOP_KEYS)

    def test_stop_block_puts_the_warning_in_the_reason(self):
        output = CODEX.format_output("Stop", "LAST CALL warning", "write the handoff")
        self.assertEqual(output["decision"], "block")
        self.assertIn("LAST CALL warning", output["reason"])
        self.assertIn("write the handoff", output["reason"])
        self.assertEqual(CODEX.format_output("Stop", None, "just this"),
                         {"decision": "block", "reason": "just this"})

    def test_additional_context_only_where_codex_accepts_it(self):
        self.assertEqual(CODEX.format_output("SessionStart", "context"),
                         {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": "context"}})
        for event in ("PostCompact", "PreCompact", "Stop"):
            self.assertEqual(CODEX.format_output(event, "context"), {"systemMessage": "context"})

    def test_no_block_once_stop_hook_is_active(self):
        # Codex has no cap on Stop blocks: re-blocking would loop forever.
        self.assertEqual(CODEX.format_output("Stop", "warn", "block", stop_hook_active=True),
                         {"systemMessage": "warn"})
        self.assertIsNone(CODEX.format_output("Stop", None, "block", stop_hook_active=True))

    def test_block_is_ignored_outside_stop_and_silence_is_none(self):
        output = CODEX.format_output("UserPromptSubmit", "hi", "no")
        self.assertNotIn("decision", output)
        self.assertIsNone(CODEX.format_output("Stop"))
        self.assertIsNone(CODEX.format_output("PostCompact", None, "no"))


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

class TestDetection(unittest.TestCase):
    CLAUDE_PATH = "/home/u/.claude/projects/-home-u-proj/%s.jsonl" % SID
    CODEX_PATH = "/home/u/.codex/sessions/2026/01/02/rollout-2026-01-02T10-00-00-%s.jsonl" % SID

    def detect(self, payload, env=None):
        return agents.detect_agent(payload, env or {}).name

    def test_transcript_location_decides(self):
        self.assertEqual(self.detect({"transcript_path": self.CLAUDE_PATH}), "claude")
        self.assertEqual(self.detect({"transcript_path": self.CODEX_PATH}), "codex")

    def test_custom_homes_are_recognised(self):
        path = "/data/cx/sessions/whatever.jsonl"
        self.assertEqual(self.detect({"transcript_path": path}, {"CODEX_HOME": "/data/cx"}), "codex")
        path = "/data/cc/projects/slug/notes.jsonl"
        self.assertEqual(self.detect({"transcript_path": path},
                                     {"CLAUDE_CONFIG_DIR": "/data/cc"}), "claude")

    def test_codex_only_payload_fields(self):
        payload = {"session_id": SID, "turn_id": "t1", "model": "gpt-test",
                   "hook_event_name": "Stop", "stop_hook_active": False}
        self.assertEqual(self.detect(payload), "codex")

    def test_stdin_keys_seen_live(self):
        # Codex SessionStart: model, but no turn_id yet.
        self.assertEqual(self.detect({"session_id": SID, "model": "gpt-test", "source": "startup",
                                      "hook_event_name": "SessionStart"}), "codex")
        # Claude Stop and PostToolUse carry keys Codex never sends.
        self.assertEqual(self.detect({"session_id": SID, "prompt_id": "p", "session_crons": [],
                                      "background_tasks": [], "stop_hook_active": False}),
                         "claude")
        self.assertEqual(self.detect({"session_id": SID, "duration_ms": 12}), "claude")

    def test_path_outranks_inherited_environment(self):
        # A Claude session launched from inside Codex inherits Codex's env.
        env = {"CODEX_THREAD_ID": OTHER_SID, "CODEX_SANDBOX": "seatbelt"}
        self.assertEqual(self.detect({"transcript_path": self.CLAUDE_PATH}, env), "claude")
        env = {"CLAUDECODE": "1", "CLAUDE_PLUGIN_ROOT": "/p"}
        self.assertEqual(self.detect({"transcript_path": self.CODEX_PATH}, env), "codex")

    def test_environment_markers_alone(self):
        self.assertEqual(self.detect({}, {"CODEX_SANDBOX": "seatbelt"}), "codex")
        self.assertEqual(self.detect({}, {"CODEX_MANAGED_PACKAGE_ROOT": "/npm/codex"}), "codex")
        self.assertEqual(self.detect({}, {"CLAUDE_PID": "123"}), "claude")
        self.assertEqual(self.detect({}, {"CLAUDECODE": "1"}), "claude")
        # Codex exports CLAUDE_PLUGIN_ROOT to plugin hooks; that is no evidence.
        self.assertEqual(self.detect({}, {"CLAUDE_PLUGIN_ROOT": "/p", "CODEX_CI": "1"}), "codex")
        both = {"CLAUDECODE": "1", "CODEX_THREAD_ID": SID}
        self.assertEqual(self.detect({"session_id": SID}, both), "codex")
        both = {"CLAUDE_CODE_SESSION_ID": SID, "CODEX_SANDBOX": "seatbelt"}
        self.assertEqual(self.detect({"session_id": SID}, both), "claude")

    def test_override_and_default(self):
        self.assertEqual(self.detect({"transcript_path": self.CLAUDE_PATH},
                                     {"LASTCALL_AGENT": "codex"}), "codex")
        self.assertEqual(self.detect({}, {}), "claude")
        self.assertEqual(agents.detect_agent({}, {}, default="codex").name, "codex")
        self.assertEqual(self.detect(None, {"LASTCALL_AGENT": "nonsense"}), "claude")

    def test_detect_method_and_registry(self):
        self.assertTrue(CODEX.detect({"turn_id": "t"}, {}))
        self.assertFalse(CODEX.detect({}, {}))
        self.assertTrue(CLAUDE.detect({"transcript_path": self.CLAUDE_PATH}, {}))
        self.assertIsNone(agents.get_agent("gemini"))
        ranked = agents.rank_agents({"transcript_path": self.CODEX_PATH}, {})
        self.assertEqual(ranked[0], (agents.EVIDENCE_PATH, CODEX))


class TestLibraryHygiene(unittest.TestCase):
    def test_usage_serialises(self):
        usage = base.Usage(tokens=1, window=None, window_source="unknown", model=None,
                           compacted=False, session_id=None, agent="claude")
        data = usage.to_dict()
        self.assertEqual(set(data), {"tokens", "window", "window_source", "model", "compacted",
                                     "session_id", "agent", "turn_id", "measured_at", "stale",
                                     "record_id", "fresh", "compaction_id",
                                     "compaction_pre_tokens"})
        json.dumps(data)

    def test_library_parses_as_python_3_9_and_uses_only_the_standard_library(self):
        package = os.path.join(LIB, "lastcall_core")
        def is_stdlib(root):
            names = getattr(sys, "stdlib_module_names", None)  # 3.10+
            if names is not None:
                return root in names
            spec = importlib.util.find_spec(root)
            origin = getattr(spec, "origin", None) or ""
            if origin in ("built-in", "frozen"):
                return True
            stdlib = sysconfig.get_paths()["stdlib"]
            return origin.startswith(stdlib) and "site-packages" not in origin
        for folder, _dirs, files in os.walk(package):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), path, feature_version=(3, 9))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        roots = [alias.name.split(".")[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and not node.level:
                        roots = [node.module.split(".")[0]]
                    else:
                        continue
                    for root in roots:
                        self.assertTrue(is_stdlib(root), "%s imports %s" % (path, root))


if __name__ == "__main__":
    unittest.main()
