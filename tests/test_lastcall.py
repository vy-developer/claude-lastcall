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
