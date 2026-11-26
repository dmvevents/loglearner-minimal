#!/usr/bin/env python3
"""Fail-closed scrubber for fleet knowledge pushes — CLI wrapper.

The engine lives in loglearner/scrub_core.py (shared with the dashboard
/export gate); this wrapper keeps the historical CLI + exit codes:
0 clean · 2 residual hits after scrub (bug) · 3 detector hit (possible
literal secret needing human review) · 4 config/job error.

Usage:
    python3 scripts/scrub.py --config state/scrub-patterns.json \
        --job state/scrub-job.json --out state/staging-scrubbed/knowledge
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loglearner.scrub_core import (  # noqa: E402,F401 — re-exported for tests
    _compile,
    collect_numbered,
    detect,
    jsonl_parse_failures,
    load_config,
    run,
    scrub_dest,
    scrub_text,
    sha256,
    verify_clean,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    try:
        sys.exit(run(args.config, args.job, args.out))
    except (KeyError, json.JSONDecodeError, OSError) as e:
        print(f"config/job error: {e}", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
