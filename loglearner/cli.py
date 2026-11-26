"""`python3 -m loglearner.cli` — the agent-facing command line (also the `loglearner` console script).

    recall "<query>" [--category X] [--status positive|negative] [--k 5] [--json] [--session ID]
        Plain text an agent can read inside a Bash call. Exit 0 on a hit, 1 on no hit.
    bank --json '<entry>' | --stdin [--bank PATH] [--id-prefix kb] [--force] [--dry-run] [--lifecycle screened|candidate]
        The sanctioned write path (schema gate, recall echo, duplicate rejection, union-max id, append).
        Exit 0 banked, 2 schema refused, 3 duplicate.
    ingest | verify | installed ...
        Pass straight through to `python3 -m loglearner.pack` (same arguments, same exit codes), so a harness can
        wrap this one module as its `loglearner` command.

Any other argument set runs the full mining CLI (loglearner.py) when it is present in this checkout; the
minimal (vendored) runtime does not carry it and says so instead of loading a missing file.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _recall(a):
    from loglearner import recall as rc
    hits = rc.recall(a.query, k=a.k, category=a.category, status=a.status)
    if a.session and hits:
        try:
            from loglearner import arms
            arms.record_exposure(a.session, [arms.arm_id_for_rule(str(h.get("rule") or h.get("text") or "")) for h in hits],
                                 project="", decision_ref="cli:recall", instinct_ids=[str(h.get("id")) for h in hits])
        except Exception as e:  # noqa: BLE001 — recall must still answer
            print(f"recall: exposure not recorded: {e}", file=sys.stderr)
    if a.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
    else:
        for h in hits:
            print(rc.format_hit(h))
            print()
    if not hits:
        print(f"no banked rule matches {a.query!r} (stores: {len(rc.store_files())} bank file(s) under {rc.banks_dir()})",
              file=sys.stderr)
        return 1
    return 0


def _bank(a):
    from loglearner import bank as bk
    try:
        raw = sys.stdin.read() if a.stdin else a.json
        entry = json.loads(raw)
    except (TypeError, ValueError) as e:
        print(f"bank refused: entry is not valid JSON ({e})", file=sys.stderr)
        return 2
    try:
        res = bk.bank(entry, bank_path=a.bank, prefix=a.id_prefix, force=a.force, dry_run=a.dry_run, lifecycle=a.lifecycle)
    except bk.BankRefused as e:
        msg = str(e)
        print(f"bank refused: {msg}", file=sys.stderr)
        return 3 if msg.startswith(("duplicate", "near-duplicate")) else 2
    if res["similar"]:
        print("nearest existing records (recall-before-append): " +
              ", ".join(f"{s['id']} ({s['jaccard']})" for s in res["similar"]), file=sys.stderr)
    print(json.dumps(res))
    return 0


def _parser():
    ap = argparse.ArgumentParser(prog="loglearner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("recall", help="search the installed banks")
    r.add_argument("query")
    r.add_argument("--category")
    r.add_argument("--status", choices=("positive", "negative"))
    r.add_argument("--k", type=int, default=5)
    r.add_argument("--json", action="store_true")
    r.add_argument("--session", help="record exposure rows for this session id (delivery, not reward)")
    b = sub.add_parser("bank", help="append a verified rule to the global bank")
    g = b.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", help="the entry as a JSON object")
    g.add_argument("--stdin", action="store_true", help="read the entry from stdin")
    b.add_argument("--bank", help="bank file (default: $LOGLEARNER_BANK or <banks dir>/global/instincts.jsonl)")
    b.add_argument("--id-prefix", default="kb")
    b.add_argument("--force", action="store_true", help="bank despite a near-duplicate (never an exact duplicate)")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--lifecycle", choices=("screened", "candidate"), default="screened",
                   help="screened (default, caller verified) or candidate (mined, awaiting human review)")
    # listed so the top-level --help names every shipped subcommand; dispatched before parsing (see main)
    for name, help_ in (("ingest", "ingest a knowledge pack (passes through to loglearner.pack)"),
                        ("verify", "verify a pack against its manifest (loglearner.pack)"),
                        ("installed", "list installed packs for a bank (loglearner.pack)")):
        sub.add_parser(name, help=help_, add_help=False).add_argument("args", nargs=argparse.REMAINDER)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("recall", "bank"):
        a = _parser().parse_args(argv)
        return _recall(a) if a.cmd == "recall" else _bank(a)
    if argv and argv[0] in ("ingest", "verify", "installed"):  # one entry point: the pack subcommands pass through
        import runpy
        sys.argv = ["loglearner.pack", *argv]
        try:
            runpy.run_module("loglearner.pack", run_name="__main__")
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
                return 1
            return int(e.code or 0)
        return 0
    legacy = ROOT / "loglearner.py"
    if argv and argv[0] in ("-h", "--help"):
        _parser().print_help()
        return 0
    if argv and legacy.is_file():
        sys.path.insert(0, str(ROOT))
        import importlib.util
        spec = importlib.util.spec_from_file_location("loglearner_main", legacy)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
        return 0
    _parser().print_help()  # no subcommand, or one this runtime does not carry: usage error
    if argv and not legacy.is_file():
        print(f"\nunknown command {argv[0]!r}; the full mining CLI (loglearner.py) is not part of this minimal runtime",
              file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
