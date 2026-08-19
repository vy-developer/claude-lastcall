#!/usr/bin/env python3
"""Tests for Last Call. Standard library only.

    python3 -m unittest discover -s tests -v

These target the things that were actually wrong in the version this was
rewritten from: absolute thresholds that could never fire, bands that never
re-armed after compaction, a transcript path derived by mangling cwd, and
subagent usage records being read as the main session's context.
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
SCRIPT = os.path.join(ROOT, "plugins", "lastcall", "scripts", "lastcall.py")
sys.path.insert(0, os.path.dirname(SCRIPT))

import lastcall as cg  # noqa: E402


def assistant_line(tokens, model="claude-sonnet-5", session="s1", sidechain=False):
    return json.dumps({
        "type": "assistant",
        "sessionId": session,
        "isSidechain": sidechain,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": tokens - 10,
                "cache_creation_input_tokens": 0,
                "output_tokens": 500,
            },
        },
    })


class TempCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lastcall-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "state")

    def transcript(self, lines):
        path = os.path.join(self.dir, "session.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def config(self, **overrides):
        # An explicit window, because the guard refuses to invent one. Tests
        # that care about resolution set it back to None themselves.
        # Scenario tests pin their own ladder so they stay meaningful when the
        # shipped defaults change; TestShippedDefaults covers those separately.
        config = dict(cg.DEFAULTS, context_window_tokens=200_000,
                      yellow_percent=70, red_percent=85)
        config.update(overrides)
        config["state_dir"] = self.state
        config["_project_dir"] = self.dir
        config["_config_path"] = None
        return config


class TestWindowResolution(unittest.TestCase):
    """The window is never guessed. Measured on a real 5.1MB transcript: a 1M
    session records its model as plain "claude-opus-5" and held 743,106 tokens,
    which is 371% of the window that identifier would imply. Inference from the
    model name is therefore not merely unreliable, it is disproven."""

    def test_config_wins(self):
        config = dict(cg.DEFAULTS, context_window_tokens=500_000)
        window, source = cg.resolve_window(config, {"max_observed": 900_000})
        self.assertEqual(window, 500_000)
        self.assertEqual(source, "config")

    def test_statusline_is_preferred_over_evidence(self):
        config = dict(cg.DEFAULTS)
        window, source = cg.resolve_window(
            config, {"window_from_statusline": 1_000_000, "max_observed": 10})
        self.assertEqual(window, 1_000_000)
        self.assertEqual(source, "statusline")

    def test_unknown_when_nothing_is_known(self):
        window, source = cg.resolve_window(dict(cg.DEFAULTS), {})
        self.assertIsNone(window)
        self.assertEqual(source, "unknown")

    def test_ambiguous_below_200k_stays_unknown(self):
        """At 150K the session could be a 200K window at 75% or a 1M window at
        15%. Guessing the first produces a false alarm that damages a session;
        the honest answer is silence."""
        self.assertIsNone(cg.window_from_evidence(150_000))
        self.assertIsNone(cg.window_from_evidence(cg.STANDARD_WINDOW))

    def test_above_200k_proves_the_extended_window(self):
        self.assertEqual(cg.window_from_evidence(743_106), cg.EXTENDED_WINDOW)

    def test_beyond_every_known_window_is_unknown_not_clamped(self):
        self.assertIsNone(cg.window_from_evidence(5_000_000))


class TestTokenCounting(unittest.TestCase):
    def test_cache_read_dominates_and_is_counted(self):
        usage = {"input_tokens": 2, "cache_read_input_tokens": 690_000,
                 "cache_creation_input_tokens": 1_000, "output_tokens": 500}
        self.assertEqual(cg.count_tokens(usage), 691_002)

    def test_output_excluded_by_default(self):
        usage = {"input_tokens": 100, "output_tokens": 900}
        self.assertEqual(cg.count_tokens(usage), 100)
        self.assertEqual(cg.count_tokens(usage, include_output=True), 1_000)

    def test_missing_fields_are_zero_not_an_error(self):
        self.assertEqual(cg.count_tokens({}), 0)


class TestShippedDefaults(unittest.TestCase):
    """The shipped ladder is a product decision, not an accident. It comes from
    a hook proven over ~50 unattended handoffs; changing it should require
    changing a test that says so out loud."""

    def test_default_ladder_is_40_55(self):
        self.assertEqual(cg.DEFAULTS["yellow_percent"], 40)
        self.assertEqual(cg.DEFAULTS["red_percent"], 55)

    def test_defaults_fire_where_the_reference_hook_fires(self):
        # The reference hook used absolute 400k/550k on a 1M window.
        config = dict(cg.DEFAULTS)
        for tokens, expected in ((399_999, "green"), (400_000, "yellow"),
                                 (549_999, "yellow"), (550_001, "red")):
            self.assertEqual(cg.band_for(tokens / 10_000.0, config), expected,
                             "%s tokens on a 1M window" % tokens)

    def test_red_blocks_by_default(self):
        zones = cg.resolve_zones(dict(cg.DEFAULTS))
        self.assertTrue([z for z in zones if z["name"] == "red"][0]["block"])


class TestBands(unittest.TestCase):
    def setUp(self):
        self.config = dict(cg.DEFAULTS, yellow_percent=70, red_percent=85)

    def test_percentage_bands(self):
        self.assertEqual(cg.band_for(10, self.config), "green")
        self.assertEqual(cg.band_for(69.9, self.config), "green")
        self.assertEqual(cg.band_for(70, self.config), "yellow")
        self.assertEqual(cg.band_for(84.9, self.config), "yellow")
        self.assertEqual(cg.band_for(85, self.config), "red")

    def test_thresholds_are_relative_so_they_fire_on_any_window(self):
        """The predecessor used absolute 600k/700k, which could never fire on a
        200k model. 70% must mean 140k there and 700k on a 1M window."""
        for window in (200_000, 1_000_000):
            tokens = int(window * 0.71)
            percent = tokens * 100.0 / window
            self.assertEqual(cg.band_for(percent, self.config), "yellow", window)


class TestReverseReader(TempCase):
    def test_reads_lines_from_the_end(self):
        path = self.transcript(["a", "b", "c"])
        got = [l.decode() for l in cg.iter_lines_reverse(path)]
        self.assertEqual(got, ["c", "b", "a"])

    def test_spans_chunk_boundaries_without_splitting_lines(self):
        lines = ["line-%04d-%s" % (i, "x" * 200) for i in range(500)]
        path = self.transcript(lines)
        got = [l.decode() for l in cg.iter_lines_reverse(path, chunk=64)]
        self.assertEqual(got, list(reversed(lines)))

    def test_crlf_transcripts_do_not_leave_a_dangling_cr(self):
        """Windows writes CRLF. Splitting on LF alone leaves \\r on every line,
        which then travels into the parsed model name and the zone name."""
        path = os.path.join(self.dir, "crlf.jsonl")
        with open(path, "wb") as handle:
            handle.write(b'{"a": 1}\r\n{"b": 2}\r\n')
        got = [l.decode() for l in cg.iter_lines_reverse(path)]
        self.assertEqual(got, ['{"b": 2}', '{"a": 1}'])

    def test_crlf_transcript_still_measures(self):
        line = assistant_line(145_000).encode()
        path = os.path.join(self.dir, "crlf-session.jsonl")
        with open(path, "wb") as handle:
            handle.write(line + b"\r\n")
        usage, model = cg.latest_usage(path, "s1")
        self.assertEqual(cg.count_tokens(usage), 145_000)
        self.assertEqual(model, "claude-sonnet-5")

    def test_empty_file_yields_nothing(self):
        path = os.path.join(self.dir, "empty.jsonl")
        open(path, "w").close()
        self.assertEqual(list(cg.iter_lines_reverse(path)), [])


class TestLatestUsage(TempCase):
    def test_takes_the_newest_record(self):
        path = self.transcript([assistant_line(100), assistant_line(50_000)])
        usage, model = cg.latest_usage(path, "s1")
        self.assertEqual(cg.count_tokens(usage), 50_000)
        self.assertEqual(model, "claude-sonnet-5")

    def test_ignores_sidechain_records(self):
        """A subagent has its own window. Counting its usage as the main
        session's context reports a number from a different conversation."""
        path = self.transcript([
            assistant_line(50_000),
            assistant_line(900_000, sidechain=True),
        ])
        usage, _ = cg.latest_usage(path, "s1")
        self.assertEqual(cg.count_tokens(usage), 50_000)

    def test_ignores_other_sessions(self):
        path = self.transcript([
            assistant_line(50_000, session="s1"),
            assistant_line(900_000, session="s2"),
        ])
        usage, _ = cg.latest_usage(path, "s1")
        self.assertEqual(cg.count_tokens(usage), 50_000)

    def test_ignores_non_assistant_and_malformed_lines(self):
        path = self.transcript([
            assistant_line(50_000),
            json.dumps({"type": "user", "message": {"usage": {"input_tokens": 999_999}}}),
            "{not json",
            "",
        ])
        usage, _ = cg.latest_usage(path, "s1")
        self.assertEqual(cg.count_tokens(usage), 50_000)

    def test_no_usage_anywhere_returns_none(self):
        path = self.transcript([json.dumps({"type": "user"})])
        usage, model = cg.latest_usage(path, "s1")
        self.assertIsNone(usage)
        self.assertIsNone(model)


class TestMeasure(TempCase):
    def test_missing_transcript_is_unknown_not_green(self):
        config = self.config()
        tokens, _w, source, _m = cg.measure(config, {"transcript_path": "/nope"})
        self.assertIsNone(tokens)
        self.assertEqual(source, "no-transcript")

    def test_current_reading_can_prove_the_window_immediately(self):
        """A session already holding 743K proves a 1M window on the very first
        Stop, without waiting for a second data point."""
        path = self.transcript([assistant_line(743_106, model="claude-opus-5")])
        config = self.config(context_window_tokens=None)
        tokens, window, source, _m = cg.measure(config, {
            "transcript_path": path, "session_id": "s1"})
        self.assertEqual(tokens, 743_106)
        self.assertEqual(window, cg.EXTENDED_WINDOW)
        self.assertIn("proven", source)

    def test_never_derives_a_path_from_cwd(self):
        """cwd may be a subdirectory of the project, and the project slug
        replaces every non-alphanumeric character. A derived path is wrong in
        two independent ways, so it must not be attempted at all."""
        config = self.config()
        # Shape taken from a real captured Stop payload: cwd was a subdirectory
        # of the project, while the transcript lived under the project root's
        # slug. Deriving the path from cwd lands on a directory that does not
        # exist, and the guard silently disables itself.
        payload = {"cwd": "/home/user/myproject/subdir", "session_id": "s1"}
        tokens, _w, source, _m = cg.measure(config, payload)
        self.assertIsNone(tokens)
        self.assertEqual(source, "no-transcript")

    def test_unknown_window_reports_no_window(self):
        path = self.transcript([assistant_line(150_000, model="claude-opus-5")])
        config = self.config(context_window_tokens=None)
        tokens, window, source, _m = cg.measure(config, {
            "transcript_path": path, "session_id": "s1"})
        self.assertEqual(tokens, 150_000)
        self.assertIsNone(window)
        self.assertEqual(source, "unknown")

    def test_configured_window_wins(self):
        path = self.transcript([assistant_line(150_000, model=None)])
        config = self.config(context_window_tokens=500_000)
        tokens, window, source, _m = cg.measure(config, {
            "transcript_path": path, "session_id": "s1"})
        self.assertEqual(tokens, 150_000)
        self.assertEqual(window, 500_000)
        self.assertEqual(source, "config")


class TestConfig(TempCase):
    def test_file_overrides_defaults(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        with open(os.path.join(self.dir, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"yellow_percent": 50, "mode": "advisory"}, fh)
        config = cg.load_config({"cwd": self.dir})
        self.assertEqual(config["yellow_percent"], 50)
        self.assertEqual(config["mode"], "advisory")
        self.assertEqual(config["red_percent"], cg.DEFAULTS["red_percent"])

    def test_env_overrides_file_field_by_field(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        with open(os.path.join(self.dir, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"yellow_percent": 50, "mode": "advisory"}, fh)
        os.environ["LASTCALL_YELLOW_PERCENT"] = "33"
        self.addCleanup(os.environ.pop, "LASTCALL_YELLOW_PERCENT", None)
        config = cg.load_config({"cwd": self.dir})
        self.assertEqual(config["yellow_percent"], 33)
        self.assertEqual(config["mode"], "advisory")  # untouched

    def test_shipped_example_config_is_valid(self):
        """The example is documentation people paste in, so it has to parse and
        it must not name a field the code ignores."""
        example = os.path.join(ROOT, "plugins", "lastcall", "lastcall.example.json")
        with open(example, encoding="utf-8") as handle:
            data = json.load(handle)
        for key in data:
            if key.startswith("_comment"):
                continue
            self.assertIn(key, cg.DEFAULTS, "example config names unknown field %r" % key)
        for key in cg.DEFAULTS:
            self.assertIn(key, data, "example config omits documented field %r" % key)

    def test_broken_config_does_not_raise(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        with open(os.path.join(self.dir, ".claude", "lastcall.json"), "w") as fh:
            fh.write("{ broken")
        config = cg.load_config({"cwd": self.dir})
        self.assertEqual(config["yellow_percent"], cg.DEFAULTS["yellow_percent"])

    def test_path_valued_env_overrides_survive_coercion(self):
        """Regression: coercion used to be inferred from the default value, so
        every field defaulting to None was treated as numeric and any path
        passed through the environment was silently discarded."""
        os.environ["LASTCALL_STATE_DIR"] = "/tmp/lastcall-example"
        os.environ["LASTCALL_TEMPLATE"] = "docs/wrapup.md"
        self.addCleanup(os.environ.pop, "LASTCALL_STATE_DIR", None)
        self.addCleanup(os.environ.pop, "LASTCALL_TEMPLATE", None)
        config = cg.load_config({"cwd": self.dir})
        self.assertEqual(config["state_dir"], "/tmp/lastcall-example")
        self.assertEqual(config["template"], "docs/wrapup.md")
        self.assertEqual(cg.state_dir(config), "/tmp/lastcall-example")

    def test_nonsense_numeric_env_falls_back_to_the_default(self):
        os.environ["LASTCALL_RED_PERCENT"] = "not-a-number"
        self.addCleanup(os.environ.pop, "LASTCALL_RED_PERCENT", None)
        config = cg.load_config({"cwd": self.dir})
        self.assertEqual(config["red_percent"], cg.DEFAULTS["red_percent"])

    def test_boolean_env_coercion(self):
        os.environ["LASTCALL_DISABLED"] = "true"
        self.addCleanup(os.environ.pop, "LASTCALL_DISABLED", None)
        self.assertTrue(cg.load_config({"cwd": self.dir})["disabled"])


class TestTemplate(TempCase):
    def test_default_template_mentions_no_project_specifics(self):
        text = cg.DEFAULT_TEMPLATE.lower()
        for leak in ("tmux", "docs/status.md", "docs/handoff", "codex", "git commit"):
            self.assertNotIn(leak, text)

    def zone(self, config, name):
        for zone in cg.resolve_zones(config):
            if zone["name"] == name:
                return zone
        raise AssertionError("no zone named %r" % name)

    def test_user_template_is_used_and_interpolated(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("at {percent:.0f}% with {remaining:,} left")
        config = self.config(template=path)
        message = cg.render(config, self.zone(config, "yellow"), 140_000, 200_000)
        self.assertIn("at 70% with 60,000 left", message)

    def test_template_with_stray_braces_still_warns(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("use {unknown_placeholder} here")
        config = self.config(template=path)
        message = cg.render(config, self.zone(config, "red"), 180_000, 200_000)
        self.assertIn("LAST CALL — RED", message)


class TestZones(TempCase):
    """Two zones called yellow and red are the default arrangement, not a
    built-in limit."""

    def test_defaults_build_yellow_and_red(self):
        zones = cg.resolve_zones(dict(cg.DEFAULTS))
        self.assertEqual([z["name"] for z in zones], ["yellow", "red"])
        self.assertEqual([z["at"] for z in zones], [40, 55])
        self.assertFalse(zones[0]["block"])
        self.assertTrue(zones[1]["block"])

    def test_custom_zones_replace_the_defaults(self):
        config = self.config(zones=[
            {"name": "nudge", "at": 40},
            {"name": "winddown", "at": 65},
            {"name": "closing", "at": 90, "block": True},
        ])
        self.assertEqual(cg.band_for(10, config), "green")
        self.assertEqual(cg.band_for(45, config), "nudge")
        self.assertEqual(cg.band_for(70, config), "winddown")
        self.assertEqual(cg.band_for(95, config), "closing")

    def test_zones_are_sorted_regardless_of_declaration_order(self):
        config = self.config(zones=[
            {"name": "high", "at": 90}, {"name": "low", "at": 30}])
        self.assertEqual([z["name"] for z in cg.resolve_zones(config)],
                         ["low", "high"])
        self.assertEqual(cg.band_for(95, config), "high")

    def test_a_single_zone_is_legitimate(self):
        config = self.config(zones=[{"name": "done", "at": 80, "block": True}])
        self.assertEqual(cg.band_for(50, config), "green")
        self.assertEqual(cg.band_for(80, config), "done")

    def test_empty_zone_list_can_never_fire(self):
        config = self.config(zones=[])
        # An empty list is indistinguishable from "not set", so the defaults
        # apply — silently disabling the tool on a typo would be worse.
        self.assertEqual(cg.band_for(95, config), "red")

    def test_malformed_zones_are_dropped_not_fatal(self):
        config = self.config(zones=[
            {"name": "ok", "at": 50},
            {"name": "no-threshold"},
            {"at": "not a number"},
            "not even a dict",
        ])
        zones = cg.resolve_zones(config)
        self.assertEqual([z["name"] for z in zones], ["ok"])

    def test_zone_without_a_name_gets_one_from_its_threshold(self):
        config = self.config(zones=[{"at": 75}])
        self.assertEqual(cg.band_for(80, config), "75%")

    def test_per_zone_inline_message(self):
        config = self.config(zones=[
            {"name": "winddown", "at": 60, "message": "STOP. Write docs/handoff.md."}])
        zone = cg.resolve_zones(config)[0]
        message = cg.render(config, zone, 130_000, 200_000)
        self.assertIn("LAST CALL — WINDDOWN", message)
        self.assertIn("STOP. Write docs/handoff.md.", message)
        self.assertNotIn("Configure this text", message)  # not the built-in

    def test_per_zone_template_file_beats_the_global_one(self):
        specific = os.path.join(self.dir, "closing.md")
        with open(specific, "w") as fh:
            fh.write("closing instructions at {percent:.0f}%")
        shared = os.path.join(self.dir, "shared.md")
        with open(shared, "w") as fh:
            fh.write("shared instructions")
        config = self.config(template=shared, zones=[
            {"name": "early", "at": 50},
            {"name": "closing", "at": 80, "template": specific},
        ])
        early, closing = cg.resolve_zones(config)
        self.assertIn("shared instructions", cg.render(config, early, 110_000, 200_000))
        self.assertIn("closing instructions at 90%",
                      cg.render(config, closing, 180_000, 200_000))

    def test_custom_headline(self):
        config = self.config(zones=[
            {"name": "z", "at": 50, "headline": "Down tools."}])
        message = cg.render(config, cg.resolve_zones(config)[0], 110_000, 200_000)
        self.assertIn("Down tools.", message)

    def test_zones_from_environment_as_json(self):
        os.environ["LASTCALL_ZONES"] = json.dumps([{"name": "solo", "at": 55}])
        self.addCleanup(os.environ.pop, "LASTCALL_ZONES", None)
        config = cg.load_config({"cwd": self.dir})
        self.assertEqual([z["name"] for z in cg.resolve_zones(config)], ["solo"])

    def test_broken_zones_json_in_environment_falls_back(self):
        os.environ["LASTCALL_ZONES"] = "{not json"
        self.addCleanup(os.environ.pop, "LASTCALL_ZONES", None)
        config = cg.load_config({"cwd": self.dir})
        self.assertEqual([z["name"] for z in cg.resolve_zones(config)],
                         ["yellow", "red"])


class TestStateAndRearm(TempCase):
    """The predecessor returned early on green, so the band file never cleared:
    after one yellow, that session never warned again — including after a
    compaction dropped it back to a few thousand tokens."""

    def run_stop(self, config, tokens, session="s1"):
        path = self.transcript([assistant_line(tokens, session=session)])
        payload = {"transcript_path": path, "session_id": session,
                   "hook_event_name": "Stop"}
        import io
        buffer = io.StringIO()
        real, sys.stdout = sys.stdout, buffer
        try:
            cg.handle_stop(config, payload)
        finally:
            sys.stdout = real
        text = buffer.getvalue()
        return json.loads(text) if text.strip() else None

    def test_warns_once_per_band_not_every_turn(self):
        config = self.config()
        self.assertIsNone(self.run_stop(config, 100_000))          # green
        self.assertIsNotNone(self.run_stop(config, 145_000))       # -> yellow
        self.assertIsNone(self.run_stop(config, 150_000))          # still yellow
        self.assertIsNotNone(self.run_stop(config, 175_000))       # -> red

    def test_green_clears_the_band_so_it_can_fire_again(self):
        config = self.config()
        self.assertIsNotNone(self.run_stop(config, 145_000))       # yellow
        self.assertIsNone(self.run_stop(config, 20_000))           # green: re-armed
        self.assertIsNotNone(self.run_stop(config, 145_000))       # yellow again

    def test_compaction_drop_rearms_without_a_green_turn(self):
        """A compact can take 175k straight to 30k. There may be no green Stop
        in between, so the drop itself has to re-arm the bands."""
        config = self.config()
        self.assertIsNotNone(self.run_stop(config, 175_000))       # red
        self.run_stop(config, 30_000)                              # compacted
        self.assertIsNotNone(self.run_stop(config, 175_000))       # red again

    def test_red_blocks_once_in_block_mode(self):
        config = self.config(mode="block_once")
        output = self.run_stop(config, 180_000)
        self.assertEqual(output["decision"], "block")

    def test_advisory_mode_never_blocks(self):
        config = self.config(mode="advisory")
        output = self.run_stop(config, 180_000)
        self.assertNotIn("decision", output)

    def test_stop_hook_active_short_circuits(self):
        config = self.config()
        path = self.transcript([assistant_line(190_000)])
        payload = {"transcript_path": path, "session_id": "s1",
                   "stop_hook_active": True}
        self.assertEqual(cg.handle_stop(config, payload), 0)

    def test_reset_rearms(self):
        config = self.config()
        self.assertIsNotNone(self.run_stop(config, 145_000))
        cg.handle_reset(config, {"session_id": "s1"})
        self.assertIsNotNone(self.run_stop(config, 145_000))

    def test_sessions_do_not_share_state(self):
        config = self.config()
        self.assertIsNotNone(self.run_stop(config, 145_000, session="s1"))
        self.assertIsNotNone(self.run_stop(config, 145_000, session="s2"))


class TestOutputContract(TempCase):
    def test_emits_required_hook_event_name(self):
        """Without hookEventName the CLI rejects the payload, the hook still
        reports success, and nothing reaches the model."""
        config = self.config()
        path = self.transcript([assistant_line(145_000)])
        import io
        buffer = io.StringIO()
        real, sys.stdout = sys.stdout, buffer
        try:
            cg.handle_stop(config, {"transcript_path": path, "session_id": "s1",
                                    "hook_event_name": "Stop"})
        finally:
            sys.stdout = real
        output = json.loads(buffer.getvalue())
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertTrue(output["suppressOutput"])


class TestEndToEnd(TempCase):
    """Run the real script the way Claude Code runs it: JSON on stdin."""

    def invoke(self, payload, event="Stop", env=None):
        environment = dict(os.environ)
        environment["LASTCALL_STATE_DIR"] = self.state
        environment["LASTCALL_CONTEXT_WINDOW_TOKENS"] = "200000"
        # Pin the ladder these scenarios were written against, so they keep
        # testing the mechanism rather than the current default numbers.
        environment["LASTCALL_YELLOW_PERCENT"] = "70"
        environment["LASTCALL_RED_PERCENT"] = "85"
        environment.pop("CLAUDE_PROJECT_DIR", None)
        environment.update(env or {})
        process = subprocess.run(
            [sys.executable, SCRIPT, event],
            input=json.dumps(payload).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
        return process

    def test_green_session_is_completely_silent(self):
        path = self.transcript([assistant_line(20_000)])
        result = self.invoke({"transcript_path": path, "session_id": "s1",
                              "hook_event_name": "Stop", "cwd": self.dir})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_yellow_session_emits_valid_json(self):
        path = self.transcript([assistant_line(145_000)])
        result = self.invoke({"transcript_path": path, "session_id": "s1",
                              "hook_event_name": "Stop", "cwd": self.dir})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        output = json.loads(result.stdout.decode())
        self.assertIn("LAST CALL — YELLOW", output["hookSpecificOutput"]["additionalContext"])

    def test_red_session_blocks_by_default(self):
        path = self.transcript([assistant_line(190_000)])
        result = self.invoke({"transcript_path": path, "session_id": "s1",
                              "hook_event_name": "Stop", "cwd": self.dir})
        output = json.loads(result.stdout.decode())
        self.assertEqual(output["decision"], "block")

    def test_garbage_stdin_never_breaks_the_session(self):
        process = subprocess.run(
            [sys.executable, SCRIPT, "Stop"], input=b"not json at all",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"")

    def test_empty_stdin_never_breaks_the_session(self):
        process = subprocess.run(
            [sys.executable, SCRIPT, "Stop"], input=b"",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(process.returncode, 0)

    def test_disabled_flag_silences_everything(self):
        path = self.transcript([assistant_line(190_000)])
        result = self.invoke(
            {"transcript_path": path, "session_id": "e2e-off",
             "hook_event_name": "Stop", "cwd": self.dir},
            env={"LASTCALL_DISABLED": "1"},
        )
        self.assertEqual(result.stdout, b"")

    def test_debug_payload_is_off_by_default(self):
        path = self.transcript([assistant_line(20_000)])
        self.invoke({"transcript_path": path, "session_id": "s1",
                     "hook_event_name": "Stop", "cwd": self.dir,
                     "last_assistant_message": "SECRET"})
        self.assertFalse(os.path.exists(os.path.join(self.state, "last-payload.json")))

    def test_debug_payload_redacts_the_assistant_message(self):
        path = self.transcript([assistant_line(20_000)])
        self.invoke(
            {"transcript_path": path, "session_id": "s1",
             "hook_event_name": "Stop", "cwd": self.dir,
             "last_assistant_message": "SECRET"},
            env={"LASTCALL_DEBUG": "1"},
        )
        target = os.path.join(self.state, "last-payload.json")
        self.assertTrue(os.path.exists(target))
        with open(target) as handle:
            self.assertNotIn("SECRET", handle.read())

    def test_unknown_window_is_silent_end_to_end(self):
        """Without a window the guard must emit nothing at all, even at a token
        count that would be red on every plausible window."""
        path = self.transcript([assistant_line(190_000)])
        result = self.invoke(
            {"transcript_path": path, "session_id": "s1",
             "hook_event_name": "Stop", "cwd": self.dir},
            env={"LASTCALL_CONTEXT_WINDOW_TOKENS": "auto"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_doctor_runs_without_a_transcript(self):
        process = subprocess.run(
            [sys.executable, SCRIPT, "doctor"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(process.returncode, 0)
        self.assertIn(b"zones", process.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTemplateWhitespace(TempCase):
    """A template's own indentation is content, not padding."""

    def test_leading_indentation_of_the_first_line_survives(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("\n\n  1. FINISH what is in flight.\n     continued here\n  2. THEN this.\n\n")
        config = self.config(template=path)
        body = cg.zone_body(config, cg.resolve_zones(config)[0])
        self.assertEqual(
            body, "  1. FINISH what is in flight.\n     continued here\n  2. THEN this.")

    def test_relay_placeholder_resolves_to_the_shipped_script(self):
        """A wrap-up template says "run {relay}" and must get a real path, so
        nobody has to hand-edit one that changes with every plugin update."""
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("step 6: run bash {relay}")
        config = self.config(template=path)
        message = cg.render(config, cg.resolve_zones(config)[0], 130_000, 200_000)
        self.assertIn(cg.RELAY_SCRIPT, message)
        self.assertTrue(os.path.isfile(cg.RELAY_SCRIPT), cg.RELAY_SCRIPT)

    def test_shipped_relay_template_renders(self):
        template = os.path.join(ROOT, "plugins", "lastcall", "templates",
                                "handoff-relay.md")
        config = self.config(template=template)
        message = cg.render(config, cg.resolve_zones(config)[0], 130_000, 200_000)
        self.assertIn("bash %s" % cg.RELAY_SCRIPT, message)
        self.assertNotIn("{relay}", message)

    def test_surrounding_blank_lines_are_still_trimmed(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("\n\nbody\n\n\n")
        config = self.config(template=path)
        self.assertEqual(cg.zone_body(config, cg.resolve_zones(config)[0]), "body")

    def test_whitespace_only_template_falls_back(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("\n   \n")
        config = self.config(template=path)
        self.assertEqual(cg.zone_body(config, cg.resolve_zones(config)[0]),
                         cg.DEFAULT_TEMPLATE)


class TestHandoverReadiness(TempCase):
    """A guard that tells the assistant to hand over, in a project where
    nothing can receive the handover, is a silent failure wearing a hat."""

    def test_unconfigured_project_reports_not_set_up(self):
        ready, checks = cg.handover_status(self.config())
        self.assertFalse(ready)
        self.assertFalse(checks["template configured"])
        self.assertFalse(checks["template invokes the relay"])

    def test_the_shipped_relay_template_counts_as_wired(self):
        """It contains {relay}, not the resolved path. Checking only for the
        resolved path reported a correct setup as broken."""
        config = self.config(template=cg.RELAY_TEMPLATE)
        ready, checks = cg.handover_status(config)
        self.assertTrue(checks["template configured"])
        self.assertTrue(checks["template invokes the relay"])

    def test_a_plain_template_is_configured_but_not_wired(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("just write some notes")
        ready, checks = cg.handover_status(self.config(template=path))
        self.assertTrue(checks["template configured"])
        self.assertFalse(checks["template invokes the relay"])
        self.assertFalse(ready)

    def test_a_zone_template_counts_too(self):
        config = self.config(zones=[
            {"name": "closing", "at": 80, "template": cg.RELAY_TEMPLATE}])
        _ready, checks = cg.handover_status(config)
        self.assertTrue(checks["template invokes the relay"])

    def test_relay_script_and_template_actually_ship(self):
        self.assertTrue(os.path.isfile(cg.RELAY_SCRIPT), cg.RELAY_SCRIPT)
        self.assertTrue(os.path.isfile(cg.RELAY_TEMPLATE), cg.RELAY_TEMPLATE)

    def test_default_message_says_handover_is_not_set_up(self):
        """With no template configured, the assistant is told the truth: the
        work stops here unless someone wires up handover."""
        config = self.config()
        message = cg.render(config, cg.resolve_zones(config)[0], 150_000, 200_000)
        self.assertIn("AUTOMATIC HANDOVER IS NOT SET UP", message)
        self.assertIn("setup", message)
        self.assertNotIn("{setup}", message)

    def test_a_configured_template_drops_that_notice(self):
        config = self.config(template=cg.RELAY_TEMPLATE)
        message = cg.render(config, cg.resolve_zones(config)[0], 150_000, 200_000)
        self.assertNotIn("AUTOMATIC HANDOVER IS NOT SET UP", message)


class TestSetupCommand(TempCase):
    def run_setup(self, cwd):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = cwd
        return subprocess.run([sys.executable, SCRIPT, "setup"],
                              input=b"", stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env, cwd=cwd)

    def test_non_interactive_setup_takes_safe_defaults(self):
        """Piped into a script with no tty it must not hang waiting for input,
        and must not silently enable an unattended spawner."""
        os.makedirs(os.path.join(self.dir, ".claude"))
        result = self.run_setup(self.dir)
        self.assertEqual(result.returncode, 0,
                         result.stdout.decode("utf-8", "replace"))
        with open(os.path.join(self.dir, ".claude", "lastcall.json")) as handle:
            written = json.load(handle)
        self.assertEqual(written["context_window_tokens"], 200_000)
        self.assertNotIn("template", written)

    def test_setup_preserves_unrelated_existing_settings(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        target = os.path.join(self.dir, ".claude", "lastcall.json")
        with open(target, "w") as fh:
            json.dump({"mode": "advisory", "yellow_percent": 33}, fh)
        self.run_setup(self.dir)
        with open(target) as handle:
            written = json.load(handle)
        self.assertEqual(written["mode"], "advisory")
        self.assertEqual(written["yellow_percent"], 33)
        self.assertTrue(os.path.isfile(target + ".bak"))


class TestSetupRecommendations(TempCase):
    """setup asks only what the machine cannot answer, and recommends the rest."""

    def git_repo(self):
        os.makedirs(os.path.join(self.dir, ".git"))
        os.makedirs(os.path.join(self.dir, ".claude"))
        return self.dir

    def run_setup(self, cwd, stdin=b""):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = cwd
        return subprocess.run([sys.executable, SCRIPT, "setup"], input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, cwd=cwd)

    def test_non_interactive_never_enables_the_relay_even_when_possible(self):
        """The recommendation would be 'yes' here — tmux/git/claude are present
        and it is a git repo. With no tty there is nobody to accept it."""
        root = self.git_repo()
        result = self.run_setup(root)
        self.assertEqual(result.returncode, 0,
                         result.stdout.decode("utf-8", "replace"))
        with open(os.path.join(root, ".claude", "lastcall.json")) as handle:
            written = json.load(handle)
        self.assertNotIn("template", written)
        self.assertFalse(os.path.exists(
            os.path.join(root, "docs", "handoff", "TEMPLATE.md")))

    def test_skeleton_carries_the_documented_sections(self):
        text = cg.HANDOFF_SKELETON
        for heading in ("Step 0", "Where things stand", "Your first work",
                        "What the last session did", "How to work here",
                        "Decided — do not re-ask", "Where everything is"):
            self.assertIn(heading, text, heading)

    def test_skeleton_demands_expected_results_not_just_commands(self):
        """The distinguishing feature of a handoff that works: Step 0 states
        what each command should return, so it can actually fail."""
        self.assertIn("EXPECTED result", cg.HANDOFF_SKELETON)

    def test_skeleton_interpolates_a_verify_command(self):
        rendered = cg.HANDOFF_SKELETON.format(
            date="2026-08-19", verify_block="```\nmake check\n```")
        self.assertIn("make check", rendered)
        self.assertIn("2026-08-19", rendered)
        self.assertNotIn("{verify_block}", rendered)


class TestGatesAndTranscript(TempCase):
    """The wrap-up has to name the project's gates and point at the raw
    transcript, or "verify before handing over" stays a pious instruction."""

    def test_unset_gates_say_so_rather_than_rendering_empty(self):
        self.assertIn("none configured", cg.format_gates(self.config()))

    def test_gates_are_listed_one_per_line(self):
        text = cg.format_gates(self.config(gates=["npm test", "npm run lint"]))
        self.assertIn("npm test", text)
        self.assertIn("npm run lint", text)
        self.assertEqual(len(text.strip().split("\n")), 2)

    def test_a_single_gate_string_is_accepted(self):
        self.assertIn("make check", cg.format_gates(self.config(gates="make check")))

    def test_gates_from_environment_as_json(self):
        os.environ["LASTCALL_GATES"] = json.dumps(["pytest -q"])
        self.addCleanup(os.environ.pop, "LASTCALL_GATES", None)
        config = cg.load_config({"cwd": self.dir})
        self.assertIn("pytest -q", cg.format_gates(config))

    def test_transcript_path_reaches_the_template(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("audit against {transcript}")
        config = self.config(template=path)
        message = cg.render(config, cg.resolve_zones(config)[0],
                            150_000, 200_000, transcript="/tmp/session.jsonl")
        self.assertIn("/tmp/session.jsonl", message)

    def test_transcript_placeholder_degrades_without_a_path(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("audit against {transcript}")
        config = self.config(template=path)
        message = cg.render(config, cg.resolve_zones(config)[0], 150_000, 200_000)
        self.assertNotIn("{transcript}", message)

    def test_shipped_relay_template_renders_every_placeholder(self):
        template = os.path.join(ROOT, "plugins", "lastcall", "templates",
                                "handoff-relay.md")
        config = self.config(template=template, gates=["make test"])
        message = cg.render(config, cg.resolve_zones(config)[0], 150_000,
                            200_000, transcript="/tmp/s.jsonl")
        for leftover in ("{relay}", "{gates}", "{transcript}", "{percent}"):
            self.assertNotIn(leftover, message, leftover)
        self.assertIn("make test", message)
        self.assertIn("/tmp/s.jsonl", message)

    def test_relay_template_covers_the_full_sequence(self):
        with open(os.path.join(ROOT, "plugins", "lastcall", "templates",
                               "handoff-relay.md")) as handle:
            text = handle.read().lower()
        for step in ("update", "gates", "audit", "commit", "hand over",
                     "subagent", "teammate", "workflow", "no user prompt"):
            self.assertIn(step, text, step)


class TestOutputEncoding(TempCase):
    """Windows consoles are not UTF-8, and this tool must not care."""

    def test_hook_payload_is_pure_ascii(self):
        """The message contains em dashes. json.dumps escapes them, so what
        reaches Claude Code is ASCII on every platform and console encoding."""
        path = self.transcript([assistant_line(150_000)])
        env = dict(os.environ)
        env["LASTCALL_STATE_DIR"] = self.state
        env["LASTCALL_CONTEXT_WINDOW_TOKENS"] = "200000"
        env.pop("CLAUDE_PROJECT_DIR", None)
        result = subprocess.run(
            [sys.executable, SCRIPT, "Stop"],
            input=json.dumps({"transcript_path": path, "session_id": "s1",
                              "hook_event_name": "Stop", "cwd": self.dir}).encode(),
            stdout=subprocess.PIPE, env=env)
        self.assertTrue(result.stdout.strip())
        result.stdout.decode("ascii")  # raises if anything slipped through
        payload = json.loads(result.stdout.decode("ascii"))
        self.assertIn("—", payload["hookSpecificOutput"]["additionalContext"])


class TestPlaceholderFormatting(TempCase):
    """Reported from real use: a template saying {percent} rendered 87.8746 and
    {tokens} rendered 439373, while the generated header alongside them showed
    88% and 439,373. Two formatters, one polished."""

    def body(self, template_text, tokens=439_373, window=500_000):
        path = os.path.join(self.dir, "t.md")
        with open(path, "w") as fh:
            fh.write(template_text)
        config = self.config(template=path)
        rendered = cg.render(config, cg.resolve_zones(config)[0], tokens, window)
        return rendered.split("\n\n", 1)[1]

    def test_bare_percent_is_rounded(self):
        self.assertEqual(self.body("{percent}"), "88")

    def test_bare_token_counts_are_grouped(self):
        self.assertEqual(self.body("{tokens}"), "439,373")
        self.assertEqual(self.body("{remaining}"), "60,627")
        self.assertEqual(self.body("{window}"), "500,000")

    def test_explicit_specs_still_win(self):
        self.assertEqual(self.body("{percent:.2f}"), "87.87")
        self.assertEqual(self.body("{tokens:,}"), "439,373")

    def test_strings_are_untouched(self):
        self.assertEqual(self.body("{zone}"), "yellow")

    def test_malformed_template_is_left_intact_not_dropped(self):
        self.assertEqual(self.body("a {bogus} b"), "a {bogus} b")


class TestEnvironmentCase(TempCase):
    """The config table documents field names in lowercase, so people copy them
    into the environment in lowercase. That used to be ignored in silence."""

    def test_uppercase_works(self):
        os.environ["LASTCALL_RED_PERCENT"] = "91"
        self.addCleanup(os.environ.pop, "LASTCALL_RED_PERCENT", None)
        self.assertEqual(cg.load_config({"cwd": self.dir})["red_percent"], 91)

    def test_lowercase_works_too(self):
        os.environ["LASTCALL_red_percent"] = "92"
        self.addCleanup(os.environ.pop, "LASTCALL_red_percent", None)
        self.assertEqual(cg.load_config({"cwd": self.dir})["red_percent"], 92)


class TestConfigValidation(TempCase):
    """A misconfigured guard that goes quiet is indistinguishable from a healthy
    one with nothing to report."""

    def load(self, raw):
        os.makedirs(os.path.join(self.dir, ".claude"), exist_ok=True)
        with open(os.path.join(self.dir, ".claude", "lastcall.json"), "w") as fh:
            json.dump(raw, fh)
        return cg.load_config({"cwd": self.dir})

    def test_wrong_type_falls_back_and_is_reported(self):
        config = self.load({"yellow_percent": "quite full"})
        self.assertEqual(config["yellow_percent"], cg.DEFAULTS["yellow_percent"])
        self.assertTrue(any("yellow_percent" in p for p in config["_problems"]))

    def test_token_count_in_a_percentage_field_is_caught(self):
        """The most likely mistake for anyone migrating from absolute bands."""
        config = self.load({"yellow_percent": 400_000})
        self.assertEqual(config["yellow_percent"], cg.DEFAULTS["yellow_percent"])
        self.assertTrue(any("PERCENTAGE" in p for p in config["_problems"]))

    def test_unknown_mode_is_caught(self):
        config = self.load({"mode": "blocking"})
        self.assertEqual(config["mode"], "block_once")
        self.assertTrue(any("mode" in p for p in config["_problems"]))

    def test_missing_template_is_reported_not_silently_ignored(self):
        config = self.load({"template": ".claude/nope.md"})
        self.assertTrue(any("does not exist" in p for p in config["_problems"]))

    def test_zone_named_off_the_builtin_list_without_its_own_copy(self):
        """resolve_zones only has built-in wording for yellow and red, so any
        other name silently renders with no headline at all."""
        config = self.load({"zones": [{"name": "winddown", "at": 50}]})
        self.assertTrue(any("winddown" in p for p in config["_problems"]))

    def test_a_zone_with_its_own_copy_is_fine(self):
        config = self.load({"zones": [
            {"name": "winddown", "at": 50, "message": "ease off"}]})
        self.assertFalse([p for p in config["_problems"] if "winddown" in p])

    def test_a_good_config_reports_no_problems(self):
        config = self.load({"yellow_percent": 50, "red_percent": 80,
                            "mode": "advisory"})
        self.assertEqual(config["_problems"], [])


class TestPruneOwnership(TempCase):
    """state_dir is user-settable. Pointing it at ~/.claude used to mean
    settings.json was deleted after the TTL, because pruning matched on the
    file extension rather than on who wrote the file."""

    def aged(self, name, content):
        os.makedirs(self.state, exist_ok=True)
        path = os.path.join(self.state, name)
        with open(path, "w") as fh:
            json.dump(content, fh)
        old = time.time() - (400 * 86400)
        os.utime(path, (old, old))
        return path

    def test_our_own_stale_state_is_pruned(self):
        path = self.aged("session.json", {"band": "red", "peak": 1})
        cg.prune_state(self.config())
        self.assertFalse(os.path.exists(path))

    def test_a_foreign_json_file_is_never_touched(self):
        path = self.aged("settings.json", {"env": {"OPENAI_API_KEY": "sk-live"}})
        cg.prune_state(self.config())
        self.assertTrue(os.path.exists(path), "deleted a file it did not write")

    def test_unparseable_json_is_left_alone(self):
        os.makedirs(self.state, exist_ok=True)
        path = os.path.join(self.state, "broken.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        old = time.time() - (400 * 86400)
        os.utime(path, (old, old))
        cg.prune_state(self.config())
        self.assertTrue(os.path.exists(path))


class TestHandoverAcrossZones(TempCase):
    """Reported from real use: a project whose RED zone invoked the relay was
    reported NOT SET UP, because the check stopped at the first readable
    template and zones sort ascending — so it only ever inspected yellow, which
    deliberately has no relay in it."""

    def layout(self, yellow_text, red_text):
        for name, text in (("winddown.md", yellow_text), ("wrapup.md", red_text)):
            with open(os.path.join(self.dir, name), "w") as fh:
                fh.write(text)
        return self.config(zones=[
            {"name": "yellow", "at": 40,
             "template": os.path.join(self.dir, "winddown.md")},
            {"name": "red", "at": 55, "block": True,
             "template": os.path.join(self.dir, "wrapup.md")},
        ])

    def test_relay_in_the_last_zone_is_found(self):
        config = self.layout("ease off, nothing about handing over",
                             "full wrap-up, finally: bash {relay}")
        _ready, checks = cg.handover_status(config)
        self.assertTrue(checks["template invokes the relay"])
        # Deliberately NOT asserting `ready`: it also requires tmux and the
        # claude CLI on PATH, which CI runners do not have. Asserting it here
        # tested the machine rather than the code, and failed all nine jobs.
        self.assertTrue(checks["template configured"])

    def test_relay_in_no_zone_is_still_reported_missing(self):
        config = self.layout("ease off", "wrap up, but never hand over")
        _ready, checks = cg.handover_status(config)
        self.assertFalse(checks["template invokes the relay"])

    def test_an_inline_zone_message_counts(self):
        config = self.config(zones=[
            {"name": "yellow", "at": 40, "message": "ease off"},
            {"name": "red", "at": 55, "message": "run bash {relay} now"}])
        _ready, checks = cg.handover_status(config)
        self.assertTrue(checks["template invokes the relay"])


class TestVerifier(TempCase):
    """A second model checking the work is the step that catches what the
    session which wrote the code cannot see about itself."""

    def test_unset_verifier_says_so_rather_than_rendering_empty(self):
        self.assertIn("none configured", cg.format_verifier(self.config()))

    def test_configured_verifier_renders_verbatim(self):
        command = 'codex exec "check the diff against docs/plans"'
        self.assertEqual(cg.format_verifier(self.config(verifier=command)), command)

    def test_verifier_reaches_the_template(self):
        path = os.path.join(self.dir, "wrap.md")
        with open(path, "w") as fh:
            fh.write("second opinion: {verifier}")
        config = self.config(template=path, verifier="codex review")
        message = cg.render(config, cg.resolve_zones(config)[0], 150_000, 200_000)
        self.assertIn("codex review", message)

    def test_detection_only_reports_tools_that_exist(self):
        for name, command, label in cg.detect_verifiers():
            self.assertTrue(shutil.which(name), "%s reported but not on PATH" % name)
            self.assertIn(name, command)
            self.assertTrue(label)

    def test_every_known_verifier_has_a_non_interactive_command(self):
        """A gate that opens an interactive REPL during wrap-up is not a gate."""
        for name, command, _label in cg.VERIFIERS:
            self.assertRegex(command, r"^%s (exec|review|-p) " % name)


class TestProjectIsolation(TempCase):
    """One machine, several projects. Config must never leak between them."""

    def project(self, name, **settings):
        root = os.path.join(self.dir, name)
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "lastcall.json"), "w") as fh:
            json.dump(settings, fh)
        return root

    def test_siblings_keep_their_own_settings(self):
        a = self.project("a", yellow_percent=30, gates=["a-gate"])
        b = self.project("b", yellow_percent=80, gates=["b-gate"])
        self.assertEqual(cg.load_config({"cwd": a})["gates"], ["a-gate"])
        self.assertEqual(cg.load_config({"cwd": b})["gates"], ["b-gate"])
        self.assertEqual(cg.load_config({"cwd": a})["yellow_percent"], 30)
        self.assertEqual(cg.load_config({"cwd": b})["yellow_percent"], 80)

    def test_a_subdirectory_resolves_to_its_own_project(self):
        a = self.project("a", gates=["a-gate"])
        deep = os.path.join(a, "src", "deep")
        os.makedirs(deep)
        self.assertEqual(cg.load_config({"cwd": deep})["gates"], ["a-gate"])

    def test_claude_project_dir_wins_over_the_working_directory(self):
        a = self.project("a", gates=["a-gate"])
        b = self.project("b", gates=["b-gate"])
        os.environ["CLAUDE_PROJECT_DIR"] = b
        self.addCleanup(os.environ.pop, "CLAUDE_PROJECT_DIR", None)
        self.assertEqual(cg.load_config({"cwd": a})["gates"], ["b-gate"])

    def test_state_files_are_keyed_by_session_not_by_project(self):
        config = self.config()
        cg.write_state(config, "session-one", {"band": "red"})
        cg.write_state(config, "session-two", {"band": "green"})
        self.assertEqual(cg.read_state(config, "session-one")["band"], "red")
        self.assertEqual(cg.read_state(config, "session-two")["band"], "green")


class TestAnswerParsing(unittest.TestCase):
    """Reported from real use: the user typed "500,000" at the context-window
    question, it was silently discarded, the default was applied, and they were
    never told. They ended up with a window they had not chosen."""

    KEYS = {"1": "a", "2": "b", "n": "no"}

    def test_a_typed_number_is_accepted(self):
        self.assertEqual(cg.parse_answer("500000", self.KEYS, True), 500_000)

    def test_thousands_separators_are_accepted(self):
        self.assertEqual(cg.parse_answer("500,000", self.KEYS, True), 500_000)
        self.assertEqual(cg.parse_answer("1_000_000", self.KEYS, True), 1_000_000)
        self.assertEqual(cg.parse_answer(" 250 000 ", self.KEYS, True), 250_000)

    def test_a_listed_option_still_wins(self):
        self.assertEqual(cg.parse_answer("1", self.KEYS, True), "1")

    def test_empty_means_take_the_recommendation(self):
        self.assertEqual(cg.parse_answer("", self.KEYS, True), "__default__")

    def test_nonsense_is_reported_not_swallowed(self):
        self.assertIsNone(cg.parse_answer("banana", self.KEYS, True))
        self.assertIsNone(cg.parse_answer("banana", self.KEYS, False))

    def test_a_number_is_refused_where_numbers_are_not_offered(self):
        self.assertIsNone(cg.parse_answer("500000", {"y": "", "n": ""}, False))

    def test_zero_and_negatives_are_not_windows(self):
        self.assertIsNone(cg.parse_answer("0", self.KEYS, True))
        self.assertIsNone(cg.parse_answer("-5", self.KEYS, True))


class TestSessionStartOnboarding(TempCase):
    """Installed but unconfigured is the same as not installed, except the user
    believes they are covered."""

    def session_start(self, cwd):
        env = dict(os.environ)
        env["LASTCALL_STATE_DIR"] = self.state
        env["CLAUDE_PROJECT_DIR"] = cwd
        return subprocess.run(
            [sys.executable, SCRIPT, "SessionStart"],
            input=json.dumps({"session_id": "s1", "cwd": cwd,
                              "hook_event_name": "SessionStart"}).encode(),
            stdout=subprocess.PIPE, env=env)

    def test_unconfigured_project_asks_in_the_session(self):
        result = self.session_start(self.dir)
        payload = json.loads(result.stdout.decode())
        text = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NOT CONFIGURED", text)
        self.assertIn("in this conversation", text)
        self.assertNotIn("{setup}", text)

    def test_it_tells_the_model_to_accept_any_window_number(self):
        text = json.loads(self.session_start(self.dir).stdout.decode())
        text = text["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Accept ANY number", text)

    def test_it_tells_the_model_gates_are_commands_not_descriptions(self):
        """The other half of the same report: a sentence was stored as a gate."""
        text = json.loads(self.session_start(self.dir).stdout.decode())
        text = text["hookSpecificOutput"]["additionalContext"]
        self.assertIn("not descriptions", text)
        self.assertIn('"pytest -q", not "run the tests"', text)

    def test_a_configured_project_is_silent(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        with open(os.path.join(self.dir, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"context_window_tokens": 500_000}, fh)
        self.assertEqual(self.session_start(self.dir).stdout, b"")

    def test_declining_silences_it(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        with open(os.path.join(self.dir, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"disabled": True}, fh)
        self.assertEqual(self.session_start(self.dir).stdout, b"")
