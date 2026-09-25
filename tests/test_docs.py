#!/usr/bin/env python3
"""The README is documentation people act on, so it is tested like code.

Every claim checked here has already been wrong once: the config table drifted
from DEFAULTS, the headline example showed 74% as YELLOW after the shipped
ladder moved to 40/55, and the test count went stale twice.
"""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
sys.path.insert(0, os.path.join(ROOT, "plugins", "lastcall", "scripts"))

import lastcall as cg  # noqa: E402


def readme():
    with open(README, encoding="utf-8") as handle:
        return handle.read()


def section(text, heading, stop=("\n## ", "\n### ")):
    start = text.index(heading) + len(heading)
    end = len(text)
    for marker in stop:
        found = text.find(marker, start)
        if found != -1:
            end = min(end, found)
    return text[start:end]


class TestReadmeMatchesTheCode(unittest.TestCase):
    def test_config_table_lists_exactly_the_real_options(self):
        table = section(readme(), "## Configuration")
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M))
        self.assertEqual(documented, set(cg.DEFAULTS),
                         "README config table has drifted from DEFAULTS")

    def test_documented_defaults_match_the_code(self):
        """Only the values the table states literally. Some cells describe
        behaviour instead — state_dir shows the path it resolves to, not the
        None it actually defaults to — and those are prose, not claims."""
        table = section(readme(), "## Configuration")
        checked = 0
        for key, shown in re.findall(r"^\| `([a-z_]+)` \| `([^`]+)` \|", table, re.M):
            try:
                literal = json.loads(shown)
            except ValueError:
                continue  # prose, not a literal
            self.assertEqual(cg.DEFAULTS[key], literal,
                             "README says %s defaults to %r" % (key, literal))
            checked += 1
        self.assertGreater(checked, 5, "config table stopped stating defaults")

    def test_every_sample_message_agrees_with_the_shipped_ladder(self):
        """A README that shows 74% as YELLOW while the tool calls it RED
        teaches the reader something false about their own install."""
        config = dict(cg.DEFAULTS)
        for band, percent in re.findall(r"LAST CALL — (\w+)\. (\d+)%", readme()):
            if band.lower() in ("yellow", "red"):
                self.assertEqual(cg.band_for(float(percent), config), band.lower(),
                                 "sample shows %s at %s%%" % (band, percent))

    def test_every_advertised_placeholder_actually_renders(self):
        advertised = set(re.findall(r"`\{(\w+)\}`", readme()))
        self.assertTrue(advertised, "no placeholders documented?")
        config = dict(cg.DEFAULTS, _project_dir=ROOT, _config_path=None)
        zone = cg.resolve_zones(config)[0]
        for name in advertised:
            probe = dict(config, template=None,
                         zones=[dict(zone, message="<<{%s}>>" % name)])
            out = cg.render(probe, cg.resolve_zones(probe)[0], 450_000,
                            1_000_000, transcript="/tmp/x.jsonl")
            self.assertNotIn("{%s}" % name, out,
                             "README advertises {%s} but it does not render" % name)

    def test_claimed_test_count_is_true(self):
        """Counted STATICALLY. Loading or running the suite from inside it
        recurses — both were tried, both hung the run."""
        claimed = re.search(r"(\d+) tests, standard library only", readme())
        self.assertIsNotNone(claimed, "README no longer states a test count")
        total = 0
        tests_dir = os.path.join(ROOT, "tests")
        for name in sorted(os.listdir(tests_dir)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            with open(os.path.join(tests_dir, name), encoding="utf-8") as handle:
                total += len(re.findall(r"^    def (test_\w+)", handle.read(), re.M))
        self.assertEqual(int(claimed.group(1)), total,
                         "README claims %s tests, the files define %d"
                         % (claimed.group(1), total))

    def test_every_local_link_resolves(self):
        for label, target in re.findall(r"\[([^\]]+)\]\(([^)#][^)]*)\)", readme()):
            if target.startswith("http"):
                continue
            self.assertTrue(os.path.exists(os.path.join(ROOT, target)),
                            "broken link: %s -> %s" % (label, target))

    def test_referenced_scripts_exist_where_the_readme_says(self):
        text = readme()
        for path in set(re.findall(r"plugins/lastcall/[\w./-]+", text)):
            self.assertTrue(os.path.exists(os.path.join(ROOT, path)), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSetupWizardDocs(unittest.TestCase):
    """The README's wizard transcript drifted from the code once already: it
    showed 1/3..3/3 after a fourth question was added. The wizard now numbers
    its questions from render.ONBOARDING_QUESTIONS, so the README is checked
    against that."""

    def questions(self):
        from lastcall_core import render
        return render.ONBOARDING_QUESTIONS

    def test_readme_shows_every_question_the_wizard_asks(self):
        questions = self.questions()
        shown = re.findall(r"^(\d+/\d+  .+)$", readme(), re.M)
        self.assertEqual(shown, ["%d/%d  %s" % (n, len(questions), q.ask)
                                 for n, q in enumerate(questions, 1)])

    def test_prose_question_count_matches(self):
        words = {"2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
                 "7": "seven", "8": "eight", "9": "nine", "10": "ten"}
        word = words[str(len(self.questions()))]
        text = readme().lower()
        self.assertTrue("%s questions" % word in text,
                        "README never says %r" % ("%s questions" % word))
        for other in sorted(set(words.values()) - {word}):
            self.assertFalse("%s questions" % other in text,
                             "README still says %r" % ("%s questions" % other))


class TestReleaseHygiene(unittest.TestCase):
    """Three commits of fixes shipped under version 1.0.0, so `/plugin update`
    compared version strings, saw no change, and reported "already at the
    latest version". The fixes were upstream and unreachable."""

    def versions(self):
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        with open(os.path.join(ROOT, "plugins", "lastcall", ".claude-plugin",
                               "plugin.json")) as fh:
            plugin = json.load(fh)
        with open(os.path.join(ROOT, "plugins", "lastcall", ".codex-plugin",
                               "plugin.json")) as fh:
            codex_plugin = json.load(fh)
        with open(os.path.join(ROOT, "plugins", "lastcall", "scripts",
                               "lastcall.py")) as fh:
            code = re.search(r'^__version__ = "([^"]+)"', fh.read(), re.M).group(1)
        return {
            "marketplace.metadata": market["metadata"]["version"],
            "marketplace.plugins[0]": market["plugins"][0]["version"],
            "plugin.json": plugin["version"],
            ".codex-plugin/plugin.json": codex_plugin["version"],
            "lastcall.py": code,
        }

    def test_every_declared_version_agrees(self):
        found = self.versions()
        self.assertEqual(len(set(found.values())), 1,
                         "version drift across declarations: %s" % found)

    def test_version_looks_like_a_release(self):
        version = self.versions()["plugin.json"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_the_relay_has_no_version_in_its_name(self):
        """It is "the relay", with no version suffix: the rework shipped as
        1.8, so a banner, help text or doc naming a second version of the relay
        names one that never existed."""
        v = "v" + "2"   # spelled apart so this file does not match itself
        pattern = re.compile(r"relay[ _-]?%s|\b%s relay" % (v, v), re.I)
        roots = [README, os.path.join(ROOT, "plugins"), os.path.join(ROOT, "tests")]
        for base in roots:
            walk = [(os.path.dirname(base), [], [os.path.basename(base)])] \
                if os.path.isfile(base) else os.walk(base)
            for directory, _dirs, names in walk:
                for name in names:
                    if name.endswith(".pyc"):
                        continue
                    path = os.path.join(directory, name)
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        found = pattern.search(fh.read())
                    self.assertIsNone(found, "%s says %r" % (path, found and found.group(0)))


class TestLineEndings(unittest.TestCase):
    """core.autocrlf=true rewrites shell scripts with CRLF on checkout, and
    bash dies on the first line: "$'\\r': command not found". Reported from a
    real WSL machine after a clean clone."""

    def test_gitattributes_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, ".gitattributes")))

    def test_shell_scripts_are_pinned_to_lf(self):
        with open(os.path.join(ROOT, ".gitattributes")) as handle:
            rules = handle.read()
        self.assertRegex(rules, r"(?m)^\*\.sh\s+text\s+eol=lf")

    def test_no_committed_shell_script_contains_a_carriage_return(self):
        for base, _dirs, names in os.walk(ROOT):
            if ".git" in base:
                continue
            for name in names:
                if not name.endswith(".sh"):
                    continue
                path = os.path.join(base, name)
                with open(path, "rb") as handle:
                    self.assertNotIn(b"\r\n", handle.read(),
                                     "%s has CRLF line endings" % path)


class TestRelayTemplateMatchesTheRelay(unittest.TestCase):
    """The wrap-up template is what the assistant believes about the relay.
    It still said the relay "never kills anything" after 1.7.0 taught the
    launcher to retire the predecessor itself."""

    def template(self):
        with open(os.path.join(ROOT, "plugins", "lastcall", "templates",
                               "handoff-relay.md"), encoding="utf-8") as fh:
            return fh.read()

    def test_it_does_not_claim_the_relay_never_kills(self):
        self.assertNotIn("never kills", self.template())

    def test_it_says_when_the_predecessor_is_retired_and_when_not(self):
        """The relay retires only on kill_predecessor / --retire-predecessor,
        never a desktop-app session, and no longer asks the successor to."""
        text = self.template()
        self.assertIn("kill_predecessor", text)
        self.assertIn("never a desktop-app session", text)
        self.assertIn("NOT\n     ASKED", text)

    def test_it_names_the_new_relay_and_cross_agent_handover(self):
        text = self.template()
        self.assertIn("{relay}", text)
        self.assertNotIn("bash {relay}", text)
        self.assertNotIn("tmux", text)
        self.assertIn("--agent codex", text)
        self.assertIn("--agent claude", text)


class TestOnboardingCoversTheFeatures(unittest.TestCase):
    """The onboarding text is what the assistant knows about this tool. It went
    stale once already: token thresholds, the window floor and model selection
    all shipped while the prompt still described an older, smaller tool, so the
    assistant could not offer them. There were also three onboarding flows that
    each asked something different; now the prompt, /lastcall:onboard and the
    wizard share render.ONBOARDING_QUESTIONS."""

    def command(self):
        with open(os.path.join(ROOT, "plugins", "lastcall", "commands",
                               "onboard.md"), encoding="utf-8") as handle:
            return handle.read()

    def texts(self):
        return {"SessionStart prompt": cg.ONBOARDING,
                "/lastcall:onboard": self.command()}

    # Only the options a user is actually onboarded onto. debug, state_dir,
    # state_ttl_days, include_output_tokens and mode are deliberately excluded:
    # they are troubleshooting knobs, not setup questions.
    MUST_MENTION = ("at_tokens", "windows", "context_window_tokens",
                    "min_window_tokens", "template", "gates", "verifier",
                    "relay", "handoff_dir", "agent", "model", "fallback_model",
                    "codex_model", "skip_permissions", "remote_control",
                    "kill_predecessor", "disabled", ".lastcall.json",
                    "AGENTS.md", "CLAUDE.md")

    def test_both_onboarding_texts_cover_every_setup_option(self):
        for where, text in self.texts().items():
            for option in self.MUST_MENTION:
                self.assertIn(option, text, "%s never mentions %r" % (where, option))

    def test_onboard_md_carries_the_shared_questions_verbatim(self):
        """The drift guard: edit the questions in render.py, then run
        `python3 plugins/lastcall/lib/lastcall_core/render.py --write-onboard`."""
        from lastcall_core import render
        text = self.command()
        begin, end = text.index(render.GENERATED_BEGIN), text.index(render.GENERATED_END)
        generated = text[begin + len(render.GENERATED_BEGIN):end].strip("\n")
        self.assertEqual(generated, render.onboard_command_block(),
                         "onboard.md drifted from render.py; regenerate it with "
                         "render.py --write-onboard")

    def test_the_prompt_is_built_from_the_same_questions(self):
        from lastcall_core import render
        self.assertIn(render.onboarding_block(render.PROMPT_DOCTOR,
                                              render.RELAY_TEMPLATE), cg.ONBOARDING)
        for text in self.texts().values():
            titles = [line.split(". ", 1)[1] for line in text.splitlines()
                      if re.match(r"^\d+\. [A-Z][A-Z -]+$", line)]
            self.assertEqual(titles, [q.title for q in render.ONBOARDING_QUESTIONS])

    def test_the_prompt_fits_what_an_agent_will_inject(self):
        """Codex caps a hook's additionalContext at roughly 2,500 tokens."""
        from lastcall_core import render
        self.assertLess(len(render.onboarding_message()), 8000)

    def test_they_say_gates_are_commands_not_descriptions(self):
        for where, text in self.texts().items():
            self.assertIn("not descriptions", text, where)

    def test_they_refuse_to_argue_about_the_window(self):
        for where, text in self.texts().items():
            self.assertIn("ANY", text, where)

    def test_they_require_explicit_consent_for_unattended(self):
        from lastcall_core import render
        self.assertFalse(render.ONBOARDING_RECOMMENDED["skip_permissions"])
        for where, text in self.texts().items():
            self.assertIn("without asking", text, where)
            self.assertIn("never enable it without a clear yes", text, where)

    def test_they_confine_writes_to_this_project(self):
        for where, text in self.texts().items():
            self.assertIn("THIS project", text, where)
            self.assertIn("never your home", text, where)

