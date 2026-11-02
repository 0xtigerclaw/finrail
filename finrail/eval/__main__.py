"""`python -m finrail.eval`: validate, run, compare, report."""
import argparse
import json
import pathlib
import sys

from . import cases as _cases
from . import compare as _compare
from . import report as _report
from . import runner, stub


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m finrail.eval",
                                description="Finrail's re-runnable evaluation of the filing checks.")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="check a case file and list its cases")
    v.add_argument("cases")

    r = sub.add_parser("run", help="dispatch every case N times and write results and a summary")
    r.add_argument("cases")
    r.add_argument("--engine", default=stub.ENGINE,
                   help="stub (default, offline), nebius, claude-cli, or any FINRAIL_ENGINE name")
    r.add_argument("--stub", action="store_true", help="same as --engine stub")
    r.add_argument("--runs", type=int, default=1, help="calls per case (default 1)")
    r.add_argument("--out", default="eval-out", help="directory for results.jsonl, summary.json and state")
    r.add_argument("--answers", default=None, help="recorded-answers file for the stub engine")
    r.add_argument("--prices", default="", metavar="MODEL",
                   help="price every call at this model's list price (for a stub run)")
    r.add_argument("--model", default="", help="FINRAIL_MODEL for a live engine")
    r.add_argument("--max-tokens", default="", help="FINRAIL_ENGINE_MAX_TOKENS for a live engine")
    r.add_argument("--label", default="", help="column title in reports (default: the engine)")
    r.add_argument("--tag", action="append", default=[], help="run only cases carrying this tag (repeatable)")
    r.add_argument("--case", action="append", default=[], help="run only this case id (repeatable)")
    r.add_argument("--quiet", action="store_true")

    c = sub.add_parser("compare", help="deltas against a baseline; exit 1 on a regression")
    c.add_argument("new")
    c.add_argument("baseline")

    t = sub.add_parser("report", help="render the Markdown tables from one or more summaries")
    t.add_argument("summaries", nargs="+")
    t.add_argument("--out", default="", help="write the Markdown here instead of stdout")
    t.add_argument("--title", default="Evaluation report")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        try:
            doc = _cases.load(args.cases)
        except _cases.CaseError as e:
            print(f"invalid: {e}", file=sys.stderr)
            return 2
        drafts = [c["id"] for c in doc["cases"] if c.get("draft")]
        print(f"{args.cases}: {len(doc['cases'])} cases valid, {len(drafts)} draft")
        for c in doc["cases"]:
            print(f"  {c['id']:48} {c['capability'].split('.')[-1]:8} {c['expect']['outcome']:8}"
                  + (" draft" if c.get("draft") else ""))
        return 0
    if args.command == "run":
        try:
            doc = _cases.load(args.cases)
            engine = stub.ENGINE if args.stub else args.engine
            summary = runner.run(doc, engine=engine, runs=args.runs, out=args.out, answers=args.answers,
                                 price_model=args.prices, tags=args.tag, ids=args.case, label=args.label,
                                 model=args.model, max_tokens=args.max_tokens,
                                 progress=None if args.quiet else print)
        except _cases.CaseError as e:
            print(f"cannot run: {e}", file=sys.stderr)
            return 2
        print(f"\n{summary['met']} of {summary['calls']} calls met their expected outcome "
              f"({summary['pass_rate'] * 100:.1f}%, 95% interval {summary['wilson_95'][0] * 100:.0f}% to "
              f"{summary['wilson_95'][1] * 100:.0f}%); wrong figures shipped: "
              f"{summary['outcomes']['wrong_figure']}. Written to {pathlib.Path(args.out) / 'summary.json'}")
        return 0 if summary["met"] == summary["calls"] else 1
    if args.command == "compare":
        new, base = _compare.load(args.new), _compare.load(args.baseline)
        result = _compare.compare(new, base)
        print(_compare.render(new, base, result))
        return 0 if result["ok"] else 1
    if args.command == "report":
        summaries = [_compare.load(p) for p in args.summaries]
        text = _report.render(summaries, title=args.title)
        if args.out:
            pathlib.Path(args.out).write_text(text)
            print(f"written to {args.out}")
        else:
            sys.stdout.write(text)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
