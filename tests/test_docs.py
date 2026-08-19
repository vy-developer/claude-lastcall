#!/usr/bin/env python3
"""The README is documentation people act on, so it is tested like code.

Every claim checked here has already been wrong once: the config table drifted
from DEFAULTS, the headline example showed 74% as YELLOW after the shipped
ladder moved to 40/55, and the test count went stale twice.
"""

import json
import os
import re
import subprocess
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
            rendered = cg.render(dict(config), zone, 450_000, 1_000_000,
                                 transcript="/tmp/x.jsonl")
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
    showed 1/3..3/3 after a fourth question was added, and none of the existing
    doc tests looked at the numbering."""

    def source(self):
        with open(os.path.join(ROOT, "plugins", "lastcall", "scripts",
                               "lastcall.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_readme_shows_every_question_the_wizard_asks(self):
        asked = sorted(set(re.findall(r'"(\d)/(\d)\s+[A-Z]', self.source())))
        self.assertTrue(asked, "no numbered prompts found in setup()")
        totals = {total for _n, total in asked}
        self.assertEqual(len(totals), 1, "wizard prompts disagree on the total")
        total = totals.pop()
        shown = sorted(set(re.findall(r"^(\d)/(\d)\s+[A-Z]", readme(), re.M)))
        self.assertEqual(len(shown), int(total),
                         "README shows %d of the wizard's %s questions"
                         % (len(shown), total))
        self.assertEqual([n for n, _t in shown], [n for n, _t in asked])

    def test_prose_question_count_matches(self):
        total = set(re.findall(r'"\d/(\d)\s+[A-Z]', self.source())).pop()
        words = {"2": "Two", "3": "Three", "4": "Four", "5": "Five",
                 "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine"}
        self.assertIn("%s questions" % words[total], readme())


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
        with open(os.path.join(ROOT, "plugins", "lastcall", "scripts",
                               "lastcall.py")) as fh:
            code = re.search(r'^__version__ = "([^"]+)"', fh.read(), re.M).group(1)
        return {
            "marketplace.metadata": market["metadata"]["version"],
            "marketplace.plugins[0]": market["plugins"][0]["version"],
            "plugin.json": plugin["version"],
            "lastcall.py": code,
        }

    def test_every_declared_version_agrees(self):
        found = self.versions()
        self.assertEqual(len(set(found.values())), 1,
                         "version drift across declarations: %s" % found)

    def test_version_looks_like_a_release(self):
        version = self.versions()["plugin.json"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")


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


class TestOnboardingCoversTheFeatures(unittest.TestCase):
    """The onboarding text is what the assistant knows about this tool. It went
    stale once already: token thresholds, the window floor and model selection
    all shipped while the prompt still described an older, smaller tool, so the
    assistant could not offer them."""

    def texts(self):
        sys.path.insert(0, os.path.join(ROOT, "plugins", "lastcall", "scripts"))
        import lastcall
        with open(os.path.join(ROOT, "plugins", "lastcall", "commands",
                               "onboard.md"), encoding="utf-8") as handle:
            command = handle.read()
        return {"SessionStart prompt": lastcall.ONBOARDING,
                "/lastcall:onboard": command}

    # Only the options a user is actually onboarded onto. debug, state_dir,
    # state_ttl_days, include_output_tokens and mode are deliberately excluded:
    # they are troubleshooting knobs, not setup questions.
    MUST_MENTION = ("at_tokens", "context_window_tokens", "min_window_tokens",
                    "template", "gates", "verifier", "relay", "handoff_dir",
                    "skip_permissions", "remote_control", "fallback_model",
                    "disabled")

    def test_both_onboarding_texts_cover_every_setup_option(self):
        for where, text in self.texts().items():
            for option in self.MUST_MENTION:
                self.assertIn(option, text, "%s never mentions %r" % (where, option))

    def test_they_say_gates_are_commands_not_descriptions(self):
        for where, text in self.texts().items():
            self.assertIn("not descriptions", text, where)

    def test_they_refuse_to_argue_about_the_window(self):
        for where, text in self.texts().items():
            self.assertIn("ANY", text, where)

    def test_they_require_explicit_consent_for_unattended(self):
        for where, text in self.texts().items():
            self.assertIn("without asking", text, where)

    def test_they_confine_writes_to_this_project(self):
        for where, text in self.texts().items():
            self.assertIn("THIS project", text, where)
