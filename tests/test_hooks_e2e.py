#!/usr/bin/env python3
"""End to end: the real hook script, run as each agent runs it.

Every test here starts plugins/lastcall/scripts/lastcall.py as a subprocess
with a synthetic Claude Code transcript or Codex rollout and the stdin payload
that agent sends for the event, then asserts the exact JSON that comes back.
The JSON contract differs per agent — Codex rejects a whole Stop payload over
one unexpected key — so each assertion says which agent it is about.

Fixtures are synthetic. The environment is scrubbed of every CLAUDE*, CODEX*
and LASTCALL_* variable, and HOME, LASTCALL_HOME, CLAUDE_CONFIG_DIR and
CODEX_HOME all point into a temporary directory, so nothing here reads or
writes a real configuration.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "lastcall")
SCRIPT = os.path.join(PLUGIN, "scripts", "lastcall.py")
STATUSLINE = os.path.join(PLUGIN, "scripts", "statusline.py")

SID = "0a1b2c3d-1111-4222-8333-444455556666"
SID2 = "0a1b2c3d-7777-4888-8999-aaaabbbbcccc"

# Everything Codex 0.153.4 accepts on Stop. Anything else voids the payload.
CODEX_STOP_KEYS = {"continue", "stopReason", "suppressOutput", "systemMessage",
                   "decision", "reason"}


def _scrubbed_env():
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith(("CLAUDE", "CODEX", "LASTCALL_", "ANTHROPIC_")):
            continue
        env[key] = value
    return env


class HookCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lastcall-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.lastcall_home = os.path.join(self.tmp, "lastcall-home")
        self.claude_dir = os.path.join(self.tmp, "claude")
        self.codex_dir = os.path.join(self.tmp, "codex")
        self.project = os.path.join(self.tmp, "work", "project")
        for path in (self.home, self.lastcall_home, self.claude_dir,
                     self.codex_dir, os.path.join(self.project, ".git")):
            os.makedirs(path)
        self.env = _scrubbed_env()
        self.env.update({
            "HOME": self.home,
            "USERPROFILE": self.home,
            "LASTCALL_HOME": self.lastcall_home,
            "CLAUDE_CONFIG_DIR": self.claude_dir,
            "CODEX_HOME": self.codex_dir,
        })
        self._msg = 0

    # -- configuration -----------------------------------------------------

    def configure(self, settings, where=".lastcall.json"):
        path = os.path.join(self.project, where)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        return path

    def configure_globally(self, settings):
        with open(os.path.join(self.lastcall_home, "config.json"), "w") as handle:
            json.dump(settings, handle)

    def state_file(self, agent, session=SID):
        return os.path.join(self.lastcall_home, "state", "%s-%s.json" % (agent, session))

    def state(self, agent, session=SID):
        with open(self.state_file(agent, session)) as handle:
            return json.load(handle)

    # -- running the hook --------------------------------------------------

    def run_hook(self, event, payload, env=None, stdin_delay_writer=None):
        environment = dict(self.env)
        environment.update(env or {})
        result = subprocess.run([sys.executable, SCRIPT, event],
                                input=json.dumps(payload).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment, cwd=self.project, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"", result.stderr.decode())
        text = result.stdout.decode("ascii")  # hook output is always ASCII
        return json.loads(text) if text.strip() else None

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env, cwd=self.project, timeout=30)

    # -- Claude Code fixtures ----------------------------------------------

    def claude_transcript(self, session=SID):
        path = os.path.join(self.claude_dir, "projects", "-work-project",
                            "%s.jsonl" % session)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            open(path, "w").close()
        return path

    def claude_reply(self, tokens, text=None, model="claude-test-1", session=SID):
        self._msg += 1
        text = text or "synthetic reply %d" % self._msg
        entry = {
            "type": "assistant", "sessionId": session, "isSidechain": False,
            "timestamp": "2026-09-25T10:%02d:00.000Z" % (self._msg % 60),
            "message": {
                "id": "msg_%04d" % self._msg, "role": "assistant", "model": model,
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 10,
                          "cache_creation_input_tokens": 0, "output_tokens": 100},
            },
        }
        self.append(self.claude_transcript(session), entry)
        return text

    def claude_user(self, session=SID):
        self.append(self.claude_transcript(session),
                    {"type": "user", "sessionId": session, "isSidechain": False,
                     "message": {"role": "user", "content": "synthetic prompt"}})

    def claude_boundary(self, post_tokens, session=SID):
        self.append(self.claude_transcript(session),
                    {"type": "system", "subtype": "compact_boundary",
                     "sessionId": session, "isSidechain": False,
                     "timestamp": "2026-09-25T11:00:00.000Z",
                     "compactMetadata": {"trigger": "auto", "preTokens": 150000,
                                         "postTokens": post_tokens}})

    def claude_payload(self, event, session=SID, **extra):
        payload = {"session_id": session, "hook_event_name": event,
                   "transcript_path": self.claude_transcript(session),
                   "cwd": self.project}
        if event == "PostToolUse":
            payload.update(tool_name="Bash", tool_input={}, tool_response={},
                           tool_use_id="toolu_1", prompt_id="p1", duration_ms=5,
                           permission_mode="default")
        elif event == "UserPromptSubmit":
            payload.update(prompt="synthetic", prompt_id="p1",
                           permission_mode="default")
        elif event == "Stop":
            payload.update(stop_hook_active=False, prompt_id="p1",
                           permission_mode="default", background_tasks=[],
                           session_crons=[])
        elif event == "SessionStart":
            payload.setdefault("source", "startup")
        payload.update(extra)
        return payload

    # -- Codex fixtures ----------------------------------------------------

    def codex_rollout(self, session=SID):
        path = os.path.join(self.codex_dir, "sessions", "2026", "09", "25",
                            "rollout-2026-09-25T10-00-00-%s.jsonl" % session)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w") as handle:
                for line in (
                        {"type": "session_meta", "payload": {"id": session}},
                        {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-test"}},
                        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1",
                                                          "model_context_window": 258400}}):
                    handle.write(json.dumps(dict(line, timestamp="2026-09-25T10:00:00Z")) + "\n")
        return path

    def codex_tokens(self, total, window=258400, session=SID, record=True):
        """One response: its token_usage_record and the token_count after it."""
        self._msg += 1
        path = self.codex_rollout(session)
        if record:
            self.append(path, {"timestamp": "2026-09-25T10:00:01Z", "type": "token_usage_record",
                               "payload": {"thread_id": session, "turn_id": "t1",
                                           "session_id": session,
                                           "response_id": "resp_%d" % self._msg,
                                           "usage": {"total_tokens": total}}})
        self.append(path, {"timestamp": "2026-09-25T10:00:02Z", "type": "event_msg",
                           "payload": {"type": "token_count", "info": {
                               "total_token_usage": {"total_tokens": total * 30},
                               "last_token_usage": {"total_tokens": total},
                               "model_context_window": window}}})

    def codex_usage_record_only(self, total, session=SID):
        self._msg += 1
        self.append(self.codex_rollout(session),
                    {"timestamp": "2026-09-25T10:00:03Z", "type": "token_usage_record",
                     "payload": {"thread_id": session, "turn_id": "t1", "session_id": session,
                                 "response_id": "resp_%d" % self._msg,
                                 "usage": {"total_tokens": total}}})

    def codex_compacted(self, session=SID):
        self.append(self.codex_rollout(session),
                    {"timestamp": "2026-09-25T10:00:04Z", "type": "compacted",
                     "payload": {"message": "", "replacement_history": []}})

    def codex_payload(self, event, session=SID, **extra):
        payload = {"session_id": session, "hook_event_name": event,
                   "transcript_path": self.codex_rollout(session),
                   "cwd": self.project, "model": "gpt-test",
                   "permission_mode": "default"}
        if event != "SessionStart":
            payload["turn_id"] = "t1"
        if event == "PostToolUse":
            payload.update(tool_name="shell", tool_input={}, tool_response={},
                           tool_use_id="call_1")
        elif event == "UserPromptSubmit":
            payload.update(prompt="synthetic")
        elif event == "Stop":
            payload.update(stop_hook_active=False, last_assistant_message="done")
        elif event == "SessionStart":
            payload.setdefault("source", "startup")
        payload.update(extra)
        return payload

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def append(path, entry):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def context_of(self, output, event):
        self.assertIsNotNone(output, "expected output on %s" % event)
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], event)
        return specific["additionalContext"]

    def assert_codex_stop_contract(self, output):
        if output is None:
            return
        self.assertNotIn("hookSpecificOutput", output)
        self.assertLessEqual(set(output), CODEX_STOP_KEYS)


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

class TestClaudeDelivery(HookCase):
    def setUp(self):
        super(TestClaudeDelivery, self).setUp()
        self.configure({"context_window_tokens": 200000})

    def stop(self, tokens, **extra):
        text = self.claude_reply(tokens)
        return self.run_hook("Stop", self.claude_payload(
            "Stop", last_assistant_message=text, **extra))

    def test_green_is_silent_on_every_event(self):
        self.claude_reply(20000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.claude_user()
        self.assertIsNone(self.run_hook("UserPromptSubmit", self.claude_payload("UserPromptSubmit")))
        self.assertIsNone(self.stop(21000))

    def test_posttooluse_warns_mid_turn_with_additional_context(self):
        self.claude_reply(90000)
        output = self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        self.assertIn("LAST CALL — YELLOW. 45%", self.context_of(output, "PostToolUse"))
        self.assertNotIn("decision", output)

    def test_user_prompt_submit_warns_with_additional_context(self):
        self.claude_reply(90000)
        output = self.run_hook("UserPromptSubmit", self.claude_payload("UserPromptSubmit"))
        self.assertIn("LAST CALL — YELLOW", self.context_of(output, "UserPromptSubmit"))

    def test_a_zone_is_announced_once_across_all_events(self):
        self.claude_reply(90000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.claude_reply(92000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.claude_user()
        self.assertIsNone(self.run_hook("UserPromptSubmit", self.claude_payload("UserPromptSubmit")))
        self.assertIsNone(self.stop(95000))

    def test_stop_warns_with_additional_context_when_it_sees_the_zone_first(self):
        output = self.stop(90000)
        self.assertIn("LAST CALL — YELLOW", self.context_of(output, "Stop"))
        self.assertNotIn("decision", output)

    def test_stop_hook_active_says_nothing_and_the_next_event_delivers(self):
        self.assertIsNone(self.stop(90000, stop_hook_active=True))
        self.claude_reply(91000)
        output = self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        self.assertIn("LAST CALL — YELLOW", self.context_of(output, "PostToolUse"))

    def test_red_warns_mid_turn_then_blocks_once_at_stop(self):
        self.claude_reply(120000)
        output = self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        self.assertIn("LAST CALL — RED", self.context_of(output, "PostToolUse"))
        self.assertNotIn("decision", output)

        output = self.stop(121000)
        self.assertEqual(output["decision"], "block")
        self.assertIn("red zone", output["reason"])
        self.assertIn("LAST CALL — RED", self.context_of(output, "Stop"))

        self.assertIsNone(self.stop(122000, stop_hook_active=True))
        self.assertIsNone(self.stop(123000))  # never twice

    def test_advisory_mode_never_blocks(self):
        self.configure({"context_window_tokens": 200000, "mode": "advisory"})
        output = self.stop(120000)
        self.assertIn("LAST CALL — RED", self.context_of(output, "Stop"))
        self.assertNotIn("decision", output)

    def test_stop_waits_for_the_record_the_hook_fired_for(self):
        """Claude Code writes the final response about 0.3 s after Stop fires.
        Reading at once would measure the previous response."""
        self.claude_reply(20000)
        self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        payload = self.claude_payload("Stop", last_assistant_message="the final words")
        process = subprocess.Popen([sys.executable, SCRIPT, "Stop"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=self.env, cwd=self.project)
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close()
        time.sleep(0.3)
        self.claude_reply(90000, text="the final words")
        out = process.stdout.read()
        process.wait(timeout=30)
        process.stdout.close()
        process.stderr.close()
        self.assertIn("LAST CALL — YELLOW", self.context_of(json.loads(out.decode()), "Stop"))

    def test_compaction_rearms_and_the_note_arrives_on_session_start(self):
        self.stop(121000)  # red, blocked
        self.claude_boundary(20000)
        self.assertIsNone(self.run_hook("PostCompact", self.claude_payload("PostCompact")))
        output = self.run_hook("SessionStart", self.claude_payload("SessionStart", source="compact"))
        note = self.context_of(output, "SessionStart")
        self.assertIn("CONTEXT COMPACTED", note)
        self.assertIn("RED warning", note)

        self.claude_reply(25000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.claude_reply(120000)
        output = self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        self.assertIn("LAST CALL — RED", self.context_of(output, "PostToolUse"))
        self.assertEqual(self.stop(121000)["decision"], "block")

    def test_compaction_seen_only_in_the_transcript_still_rearms(self):
        self.claude_reply(90000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.claude_boundary(15000)
        self.claude_reply(16000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.claude_reply(90000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))

    def test_compaction_note_can_be_switched_off(self):
        self.configure({"context_window_tokens": 200000, "compaction_note": False})
        self.claude_reply(50000)
        self.assertIsNone(self.run_hook(
            "SessionStart", self.claude_payload("SessionStart", source="compact")))

    def test_a_resumed_session_is_not_warned_twice(self):
        self.claude_reply(90000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.assertIsNone(self.run_hook(
            "SessionStart", self.claude_payload("SessionStart", source="resume")))
        self.claude_reply(95000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))

    def test_an_unchanged_transcript_is_not_read_again(self):
        self.claude_reply(50000)
        self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        before = self.state("claude")
        os.utime(self.state_file("claude"), (1, 1))
        self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        self.assertEqual(os.path.getmtime(self.state_file("claude")), 1)
        self.assertEqual(self.state("claude")["sig"], before["sig"])

    def test_the_status_line_window_survives_the_hooks(self):
        self.configure({})
        subprocess.run([sys.executable, STATUSLINE],
                       input=json.dumps({"session_id": SID, "cwd": self.project,
                                         "context_window_size": 1000000,
                                         "context_used_tokens": 10}).encode(),
                       stdout=subprocess.PIPE, env=self.env, cwd=self.project, check=True)
        self.claude_reply(150000)
        # 15% of the 1M window the status line reported: green, silent.
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        state = self.state("claude")
        self.assertEqual(state["window_from_statusline"], 1000000)
        self.assertEqual(state["window_source"], "statusline")

    def test_pre_2_state_is_picked_up_so_an_upgrade_does_not_rewarn(self):
        legacy = os.path.join(self.home, ".claude", "lastcall")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "%s.json" % SID), "w") as handle:
            json.dump({"band": "yellow", "peak": 88000, "max_observed": 88000}, handle)
        self.claude_reply(90000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.assertEqual(self.state("claude")["band"], "yellow")

    def test_a_subagent_tool_call_does_not_use_up_the_warning(self):
        self.claude_reply(90000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload(
            "PostToolUse", agent_id="agent-1", agent_type="general-purpose")))
        output = self.run_hook("PostToolUse", self.claude_payload("PostToolUse"))
        self.assertIn("LAST CALL — YELLOW", self.context_of(output, "PostToolUse"))

    def test_pre_compact_and_subagent_stop_are_accepted_quietly(self):
        self.claude_reply(150000)
        self.assertIsNone(self.run_hook("PreCompact", self.claude_payload("PreCompact")))
        self.assertIsNone(self.run_hook("SubagentStop", self.claude_payload("SubagentStop")))


class TestClaudeZeroConfig(HookCase):
    """Installed, never configured: it must do something correct."""

    def test_unknown_window_assumes_200k_and_says_so(self):
        self.claude_reply(90000)
        text = self.context_of(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")),
                               "PostToolUse")
        self.assertIn("LAST CALL — YELLOW. 45%", text)
        self.assertIn("assumed the", text)

    def test_an_assumed_window_never_blocks(self):
        text = self.claude_reply(120000)
        output = self.run_hook("Stop", self.claude_payload("Stop", last_assistant_message=text))
        self.assertIn("LAST CALL — RED", self.context_of(output, "Stop"))
        self.assertNotIn("decision", output)

    def test_zones_in_tokens_block_whatever_the_window(self):
        self.configure({"zones": [{"name": "red", "at_tokens": 100000, "block": True}]})
        text = self.claude_reply(120000)
        output = self.run_hook("Stop", self.claude_payload("Stop", last_assistant_message=text))
        self.assertEqual(output["decision"], "block")
        self.assertNotIn("assumed", self.context_of(output, "Stop"))

    def test_beyond_200k_the_window_is_proven_1m(self):
        self.claude_reply(420000)
        text = self.context_of(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")),
                               "PostToolUse")
        self.assertIn("LAST CALL — YELLOW. 42%", text)
        self.assertNotIn("assumed", text)

    def test_a_1m_model_in_settings_counts(self):
        with open(os.path.join(self.claude_dir, "settings.json"), "w") as handle:
            json.dump({"model": "claude-test-1[1m]"}, handle)
        self.claude_reply(150000)
        self.assertIsNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))
        self.assertIn("configured model", self.state("claude")["window_source"])

    def test_a_1m_model_for_a_different_model_does_not(self):
        with open(os.path.join(self.claude_dir, "settings.json"), "w") as handle:
            json.dump({"model": "claude-other-9[1m]"}, handle)
        self.claude_reply(90000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.claude_payload("PostToolUse")))


class TestOnboarding(HookCase):
    def test_offered_once_per_project_not_every_session(self):
        output = self.run_hook("SessionStart", self.claude_payload("SessionStart"))
        text = self.context_of(output, "SessionStart")
        self.assertIn("NOT CONFIGURED", text)
        self.assertIn("AGENTS.md", text)
        self.assertIn("CLAUDE.md", text)
        self.assertIn(".lastcall.json", text)
        self.assertIsNone(self.run_hook(
            "SessionStart", self.claude_payload("SessionStart", session=SID2)))
        self.assertIsNone(self.run_hook(
            "SessionStart", self.codex_payload("SessionStart", session=SID2)))

    def test_codex_gets_it_in_its_own_shape(self):
        output = self.run_hook("SessionStart", self.codex_payload("SessionStart"))
        self.assertEqual(set(output), {"hookSpecificOutput"})
        self.assertIn("NOT CONFIGURED", self.context_of(output, "SessionStart"))

    def test_a_global_config_counts_as_configured(self):
        self.configure_globally({"mode": "advisory"})
        self.assertIsNone(self.run_hook("SessionStart", self.claude_payload("SessionStart")))

    def test_the_inert_global_config_the_installer_writes_does_not_count(self):
        """Review finding: `lastcall install` writes ~/.lastcall/config.json
        with every option parked under "_example" — nothing active — and that
        file alone suppressed the onboarding offer for good."""
        sys.path.insert(0, os.path.join(PLUGIN, "lib"))
        from lastcall_core.cli import global_config_text
        with open(os.path.join(self.lastcall_home, "config.json"), "w") as handle:
            handle.write(global_config_text())
        output = self.run_hook("SessionStart", self.claude_payload("SessionStart"))
        self.assertIn("NOT CONFIGURED", self.context_of(output, "SessionStart"))

    def test_a_broken_config_file_still_counts_as_configured(self):
        with open(os.path.join(self.lastcall_home, "config.json"), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(self.run_hook("SessionStart", self.claude_payload("SessionStart")))

    def test_never_in_the_home_directory(self):
        payload = self.claude_payload("SessionStart", cwd=self.home)
        environment = dict(self.env)
        result = subprocess.run([sys.executable, SCRIPT, "SessionStart"],
                                input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
                                env=environment, cwd=self.home)
        self.assertEqual(result.stdout, b"")

    def test_resume_and_compact_never_onboard(self):
        for source in ("resume", "compact"):
            output = self.run_hook("SessionStart", self.claude_payload("SessionStart", source=source))
            text = output and self.context_of(output, "SessionStart") or ""
            self.assertNotIn("NOT CONFIGURED", text)


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

class TestCodexDelivery(HookCase):
    """Zero config: Codex writes the window (258,400 here) into the rollout."""

    def stop(self, total, **extra):
        self.codex_tokens(total)
        output = self.run_hook("Stop", self.codex_payload("Stop", **extra))
        self.assert_codex_stop_contract(output)
        return output

    def test_green_is_silent(self):
        self.codex_tokens(30000)
        self.assertIsNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))
        self.assertIsNone(self.stop(31000))

    def test_posttooluse_warns_with_additional_context(self):
        self.codex_tokens(110000)
        output = self.run_hook("PostToolUse", self.codex_payload("PostToolUse"))
        self.assertEqual(set(output), {"hookSpecificOutput"})
        text = self.context_of(output, "PostToolUse")
        self.assertIn("LAST CALL — YELLOW. 43%", text)
        self.assertIn("258,400", text)
        self.assertNotIn("assumed", text)

    def test_posttooluse_reads_the_usage_record_written_before_token_count(self):
        self.codex_tokens(40000)
        self.codex_usage_record_only(110000)
        output = self.run_hook("PostToolUse", self.codex_payload("PostToolUse"))
        self.assertIn("LAST CALL — YELLOW", self.context_of(output, "PostToolUse"))

    def test_user_prompt_submit_warns_with_additional_context(self):
        self.codex_tokens(110000)
        output = self.run_hook("UserPromptSubmit", self.codex_payload("UserPromptSubmit"))
        self.assertIn("LAST CALL — YELLOW", self.context_of(output, "UserPromptSubmit"))

    def test_stop_warning_is_a_block_and_never_carries_hook_specific_output(self):
        output = self.stop(110000)
        self.assertEqual(output["decision"], "block")
        self.assertIn("LAST CALL — YELLOW", output["reason"])

    def test_stop_hook_active_suppresses_everything(self):
        self.assertIsNone(self.stop(150000, stop_hook_active=True))
        self.codex_tokens(151000)
        output = self.run_hook("PostToolUse", self.codex_payload("PostToolUse"))
        self.assertIn("LAST CALL — RED", self.context_of(output, "PostToolUse"))

    def test_red_blocks_once_with_the_full_wrap_up(self):
        output = self.stop(150000)
        self.assertEqual(output["decision"], "block")
        self.assertIn("LAST CALL — RED", output["reason"])
        self.assertIn("write the handoff before stopping", output["reason"])
        self.assertIsNone(self.stop(151000, stop_hook_active=True))
        self.assertIsNone(self.stop(152000))

    def test_red_announced_mid_turn_still_blocks_once_at_stop(self):
        self.codex_tokens(150000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))
        output = self.stop(151000)
        self.assertEqual(output["decision"], "block")
        self.assertIsNone(self.stop(152000))

    def test_a_zone_is_announced_once_across_events(self):
        self.codex_tokens(110000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))
        self.codex_tokens(111000)
        self.assertIsNone(self.run_hook("UserPromptSubmit", self.codex_payload("UserPromptSubmit")))
        self.assertIsNone(self.stop(112000))

    def test_compaction_stale_reading_then_note_then_rearm(self):
        self.stop(150000)  # red, blocked
        self.codex_compacted()
        # The newest reading predates the compaction: never warn on it.
        self.assertIsNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))
        self.assertIsNone(self.run_hook("PostCompact", self.codex_payload("PostCompact")))
        output = self.run_hook("SessionStart", self.codex_payload("SessionStart", source="compact"))
        self.assertEqual(set(output), {"hookSpecificOutput"})
        self.assertIn("CONTEXT COMPACTED", self.context_of(output, "SessionStart"))
        self.codex_tokens(18000)
        self.assertIsNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))
        self.assertEqual(self.stop(150000)["decision"], "block")

    def test_a_resumed_session_is_not_warned_twice(self):
        self.stop(110000)
        self.assertIsNone(self.run_hook(
            "SessionStart", self.codex_payload("SessionStart", source="resume")))
        self.codex_tokens(112000)
        self.assertIsNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))

    def test_the_rollout_window_beats_a_claude_sized_global_config(self):
        self.configure_globally({"context_window_tokens": 1000000})
        self.codex_tokens(110000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))
        self.assertEqual(self.state("codex")["window_source"], "transcript")

    def test_state_is_kept_per_agent(self):
        self.codex_tokens(110000)
        self.run_hook("PostToolUse", self.codex_payload("PostToolUse"))
        self.assertTrue(os.path.isfile(self.state_file("codex")))
        self.assertFalse(os.path.exists(self.state_file("claude")))


# --------------------------------------------------------------------------
# Configuration and doctor
# --------------------------------------------------------------------------

class TestConfigLayers(HookCase):
    def test_global_config_applies_to_every_project(self):
        self.configure_globally({"zones": [{"name": "yellow", "at_tokens": 50000}]})
        self.codex_tokens(60000)
        output = self.run_hook("PostToolUse", self.codex_payload("PostToolUse"))
        self.assertIn("60,000 of 258,400", self.context_of(output, "PostToolUse"))

    def test_project_config_beats_global(self):
        self.configure_globally({"disabled": True})
        self.configure({"disabled": False})
        self.codex_tokens(110000)
        self.assertIsNotNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse")))

    def test_environment_beats_project(self):
        self.configure({"disabled": False})
        self.codex_tokens(110000)
        self.assertIsNone(self.run_hook("PostToolUse", self.codex_payload("PostToolUse"),
                                        env={"LASTCALL_DISABLED": "1"}))

    def test_codex_directory_config_is_found(self):
        self.configure({"mode": "advisory"}, where=os.path.join(".codex", "lastcall.json"))
        self.assertIsNone(self.stop_codex(150000).get("hookSpecificOutput"))
        self.assertIn("LAST CALL — RED", self.stop_codex_reason)

    def stop_codex(self, total):
        self.codex_tokens(total)
        output = self.run_hook("Stop", self.codex_payload("Stop"))
        self.stop_codex_reason = output["reason"]
        # advisory: a warning block, not the red-zone hold
        self.assertNotIn("write the handoff before stopping", output["reason"])
        return output

    def test_config_is_found_from_a_subdirectory(self):
        self.configure({"zones": [{"name": "yellow", "at_tokens": 50000}]})
        deep = os.path.join(self.project, "src", "deep")
        os.makedirs(deep)
        self.codex_tokens(60000)
        self.assertIsNotNone(self.run_hook(
            "PostToolUse", self.codex_payload("PostToolUse", cwd=deep)))

    def test_doctor_reports_typos_and_shadowed_files(self):
        self.configure({"yelow_percent": 30})
        self.configure({}, where=os.path.join(".claude", "lastcall.json"))
        result = self.run_script("doctor")
        out = result.stdout.decode()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertRegex(out, r'PROBLEM\s+: unknown setting "yelow_percent".*did you mean "yellow_percent"')
        self.assertRegex(out, r"PROBLEM\s+: .*lastcall\.json is ignored")

    def test_comment_keys_are_not_typos(self):
        self.configure({"_comment": "notes", "mode": "advisory"})
        self.assertNotIn(b"PROBLEM", self.run_script("doctor").stdout)


class TestDoctorAcrossAgents(HookCase):
    def test_doctor_measures_a_codex_rollout(self):
        self.codex_tokens(110000)
        result = self.run_script("doctor", self.codex_rollout())
        out = result.stdout.decode()
        self.assertEqual(result.returncode, 0, out + result.stderr.decode())
        self.assertIn("agent         : codex", out)
        self.assertIn("258,400 tokens (transcript)", out)
        self.assertIn("band          : YELLOW", out)
        self.assertIn("compaction    : none", out)

    def test_doctor_reports_a_codex_compaction(self):
        self.codex_tokens(150000)
        self.codex_compacted()
        out = self.run_script("doctor", self.codex_rollout()).stdout.decode()
        self.assertIn("compaction    : NEWER", out)

    def test_doctor_measures_a_claude_transcript(self):
        self.claude_reply(90000)
        result = self.run_script("doctor", self.claude_transcript())
        out = result.stdout.decode()
        self.assertEqual(result.returncode, 0, out + result.stderr.decode())
        self.assertIn("agent         : claude", out)
        self.assertIn("ASSUMED", out)
        self.assertIn("band          : YELLOW", out)

    def test_doctor_uses_the_status_line_window_for_that_session(self):
        subprocess.run([sys.executable, STATUSLINE],
                       input=json.dumps({"session_id": SID, "cwd": self.project,
                                         "context_window_size": 1000000,
                                         "context_used_tokens": 10}).encode(),
                       stdout=subprocess.PIPE, env=self.env, cwd=self.project, check=True)
        self.claude_reply(90000)
        out = self.run_script("doctor", self.claude_transcript()).stdout.decode()
        self.assertIn("1,000,000 tokens (statusline)", out)
        self.assertIn("band          : GREEN", out)


class TestPluginManifests(unittest.TestCase):
    def load(self, *parts):
        with open(os.path.join(PLUGIN, *parts), encoding="utf-8") as handle:
            return json.load(handle)

    def test_hooks_cover_every_event_with_the_stable_entry_point(self):
        hooks = self.load("hooks", "hooks.json")["hooks"]
        self.assertEqual(set(hooks), {"PostToolUse", "UserPromptSubmit", "Stop",
                                      "SessionStart", "PostCompact"})
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    # Through the launcher, which finds a working Python
                    # under a desktop app's minimal PATH.
                    self.assertEqual(
                        hook["command"],
                        'sh "${CLAUDE_PLUGIN_ROOT}/scripts/lastcall-hook" %s' % event)
                    # Codex on Windows runs commandWindows instead (Claude
                    # Code ignores the field).
                    self.assertEqual(
                        hook["commandWindows"],
                        'py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/lastcall.py" %s' % event)
        for event in ("PostToolUse", "UserPromptSubmit"):
            self.assertLessEqual(hooks[event][0]["hooks"][0]["timeout"], 5)

    def test_codex_manifest_mirrors_the_claude_one(self):
        claude = self.load(".claude-plugin", "plugin.json")
        codex = self.load(".codex-plugin", "plugin.json")
        for key in ("name", "version", "description"):
            self.assertEqual(codex[key], claude[key])
        self.assertEqual(codex["hooks"], "./hooks/hooks.json")
        self.assertTrue(codex["interface"]["displayName"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
