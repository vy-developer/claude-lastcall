"""`status` and `tidy` subcommands over lastcall_core.sessions.

    PYTHONPATH=plugins/lastcall/lib python3 -m lastcall_core.cli_sessions status [--json]
    PYTHONPATH=plugins/lastcall/lib python3 -m lastcall_core.cli_sessions tidy [--plan FILE]
    PYTHONPATH=plugins/lastcall/lib python3 -m lastcall_core.cli_sessions tidy --apply FILE

tidy is read-only unless --apply is given, and --apply only takes a plan file
written earlier by `tidy --plan` (and reviewed by you).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import sessions as S


def _homes(args):
    return S.claude_home(args.claude_home), S.codex_home(args.codex_home)


def _agents(args):
    return (args.agent,) if getattr(args, "agent", None) else (S.CLAUDE, S.CODEX)


def cmd_status(args) -> int:
    c_home, x_home = _homes(args)
    recs = S.live_sessions(c_home, x_home, agents=_agents(args))
    if args.surface:
        recs = [r for r in recs if r.surface == args.surface]
    if args.json:
        print(json.dumps([r.to_dict() for r in recs], indent=2, ensure_ascii=False))
    else:
        print(S.render_status(recs))
    return 0


def cmd_tidy(args) -> int:
    c_home, x_home = _homes(args)
    if args.apply:
        if args.plan:
            print("tidy: use --plan or --apply, not both", file=sys.stderr)
            return 2
        try:
            result = S.apply_plan(args.apply, c_home, x_home, dry_run=args.dry_run)
        except S.PlanError as exc:
            print("tidy: %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            verb = "would rename" if args.dry_run else "renamed"
            print("%s %d session(s), skipped %d." % (verb, len(result["applied"]),
                                                     len(result["skipped"])))
            for s in result["skipped"]:
                print("  skip %s %s: %s" % (s["agent"], s["session_id"], s["reason"]))
            for b in result["backups"]:
                print("  backup: %s" % b)
        return 0

    recs = S.all_sessions(c_home, x_home, agents=_agents(args))
    if args.surface:
        recs = [r for r in recs if r.surface == args.surface]
    if args.project:
        needle = args.project.lower()
        recs = [r for r in recs if needle in (r.project or r.cwd or "").lower()]
    if args.older_than:
        cutoff = time.time() - args.older_than * 86400
        recs = [r for r in recs if (r.updated_at or 0) < cutoff]
    plan = S.build_plan(recs, c_home, x_home, include_desktop=args.include_desktop)
    if args.plan:
        with open(args.plan, "w", encoding="utf-8") as fh:
            fh.write(S.plan_to_json(plan))
    if args.json:
        sys.stdout.write(S.plan_to_json(plan))
    else:
        print(S.render_plan(plan, group_by=args.group_by))
        if args.plan:
            print("\nPlan written to %s. Review it, then: tidy --apply %s"
                  % (args.plan, args.plan))
        else:
            print("\nRead-only. To act on this: tidy --plan plan.json, review, "
                  "then tidy --apply plan.json")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lastcall", description=__doc__.split("\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--claude-home", help="default: $LASTCALL_CLAUDE_HOME or ~/.claude")
    common.add_argument("--codex-home", help="default: $LASTCALL_CODEX_HOME or ~/.codex")
    common.add_argument("--agent", choices=(S.CLAUDE, S.CODEX))
    common.add_argument("--surface", choices=S.SURFACES)
    common.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    st = sub.add_parser("status", parents=[common], help="live sessions")
    st.set_defaults(func=cmd_status)

    td = sub.add_parser("tidy", parents=[common], help="propose names for old chats")
    td.add_argument("--project", help="only projects whose path contains this")
    td.add_argument("--older-than", type=float, metavar="DAYS", default=0)
    td.add_argument("--group-by", choices=("project", "surface"), default="project")
    td.add_argument("--include-desktop", action="store_true",
                    help="also rename desktop-app sessions (the apps keep their own titles)")
    td.add_argument("--plan", metavar="FILE", help="write the proposal to FILE for review")
    td.add_argument("--apply", metavar="FILE", help="apply a reviewed plan file")
    td.add_argument("--dry-run", action="store_true", help="with --apply: change nothing")
    td.set_defaults(func=cmd_tidy)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
