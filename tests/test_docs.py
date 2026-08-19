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
