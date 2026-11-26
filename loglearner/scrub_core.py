"""Fail-closed scrub engine — the single implementation behind every path
that lets bytes leave this host (scripts/scrub.py CLI pushes and the
dashboard /export endpoint).

Pattern config is intentionally NOT committed (see
scripts/scrub-patterns.example.json for the schema); the real config lives in
a gitignored local file so the repo never carries concrete values.

Config keys: replacements (class/regex/placeholder), numbered
(class/regex/placeholder_prefix — globally deterministic numbering),
detectors (class/regex — possible literal secrets, block + human review),
file_denylist, detector_allowlist. BUILTIN_REPLACEMENTS are appended to every
config's replacements: shapes that identify a host or its user and look the
same everywhere, so no config has to list them.
"""

import hashlib
import json
import re
from pathlib import Path

# Classes are applied in config order; put more-specific patterns first
# (e.g. a prefixed node-id class before a bare instance-id class).


def _compile(entry):
    flags = re.IGNORECASE if "i" in entry.get("flags", "") else 0
    return re.compile(entry["regex"], flags)


# A user's home directory, plain (/home/<user>, /Users/<user>) or in Claude Code's project-directory encoding
# (-home-<user>-<path>, the directory names under ~/.claude/projects/: every character of the path that is not a
# letter or digit becomes "-", so a user name first.last reads first-last there). Found by a consumer of a vendored
# build whose registry data carried the encoded form of the build host's path and user; no configured pattern saw it.
# The encoded form cannot show where a dotted user name ends, so the whole encoded directory token is replaced (the
# safe direction); the class does not match after a preceding "-" (a --home-dir option is not an encoded path).
# Applied after the configured classes (more-specific patterns first); the residual scan covers them like any class.
BUILTIN_REPLACEMENTS = [
    {"class": "builtin-home-path", "regex": r"(?<![A-Za-z0-9_./-])/(?:home|Users)/[A-Za-z0-9_.-]+", "placeholder": "~"},
    {"class": "builtin-home-path-encoded", "regex": r"(?<![A-Za-z0-9_-])-(?:home|Users)-[A-Za-z0-9-]+",
     "placeholder": "-HOME"},
]


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    for key in ("replacements", "numbered", "detectors"):
        cfg.setdefault(key, [])
    cfg["replacements"] = list(cfg["replacements"]) + [dict(e) for e in BUILTIN_REPLACEMENTS]
    cfg.setdefault("file_denylist", [])
    cfg.setdefault("detector_allowlist", [])
    for entry in cfg["replacements"] + cfg["numbered"] + cfg["detectors"]:
        entry["_re"] = _compile(entry)
    return cfg


def collect_numbered(cfg, texts):
    """Pass 1: global, deterministic numbering of unique matches per class."""
    mapping = {}
    for entry in cfg["numbered"]:
        values = set()
        for text in texts:
            values.update(m.group(0) for m in entry["_re"].finditer(text))
        prefix = entry["placeholder_prefix"]
        mapping[prefix] = {
            v: f"<{prefix}_{i + 1}>" for i, v in enumerate(sorted(values))
        }
    return mapping


def scrub_text(cfg, text, numbered_map):
    counts = {}
    for entry in cfg["replacements"]:
        text, n = entry["_re"].subn(entry["placeholder"], text)
        if n:
            counts[entry["class"]] = counts.get(entry["class"], 0) + n
    for entry in cfg["numbered"]:
        table = numbered_map[entry["placeholder_prefix"]]

        def _sub(m):
            return table[m.group(0)]

        text, n = entry["_re"].subn(_sub, text)
        if n:
            counts[entry["class"]] = counts.get(entry["class"], 0) + n
    return text, counts


def scrub_dest(cfg, dest, numbered_map):
    """Staged file paths can leak class values too (e.g. a customer name in a
    filename) — scrub them with filename-safe placeholders (no angle brackets)."""
    clean, _ = scrub_text(cfg, dest, numbered_map)
    return re.sub(r"[<>]", "", clean)


def detect(cfg, text, where):
    hits = []
    for entry in cfg["detectors"]:
        for m in entry["_re"].finditer(text):
            frag = m.group(0)
            if frag in cfg["detector_allowlist"]:
                continue
            # Never echo the candidate secret itself — position + class only.
            hits.append((entry["class"], where, m.start()))
    return hits


def verify_clean(cfg, text):
    """Residual scan: no replacement/numbered pattern may match the output."""
    residual = []
    for entry in cfg["replacements"] + cfg["numbered"]:
        n = len(entry["_re"].findall(text))
        if n:
            residual.append((entry["class"], n))
    return residual


def jsonl_parse_failures(text):
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad += 1
    return bad


def sha256(data):
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def run(cfg_path, job_path, out_dir):
    """Batch job mode (the scripts/scrub.py CLI): scrub a job manifest of
    files into out_dir. Exit-code semantics: 0 clean, 2 residual, 3 detector."""
    cfg = load_config(cfg_path)
    job = json.loads(Path(job_path).read_text())
    out_root = Path(out_dir)

    files = []
    for spec in job["files"]:
        src = Path(spec["src"]).expanduser()
        if src.name in cfg["file_denylist"]:
            print(f"  DENYLIST skip: {src.name}")
            continue
        if not src.is_file():
            print(f"  MISSING (skipped, noted): {src}")
            continue
        files.append((src, spec["dest"]))

    texts = {src: src.read_text(errors="replace") for src, _ in files}
    # Dest paths participate in numbering/verification like any other text.
    numbered_map = collect_numbered(
        cfg, list(texts.values()) + [d for _, d in files]
    )

    manifest, all_detect, failed = [], [], False
    for src, dest in files:
        dest = scrub_dest(cfg, dest, numbered_map)
        text = texts[src]
        all_detect.extend(detect(cfg, text, str(src)))
        scrubbed, counts = scrub_text(cfg, text, numbered_map)

        residual = verify_clean(cfg, scrubbed) + verify_clean(cfg, dest)
        if residual:
            print(f"  RESIDUAL in {dest}: {residual}")
            failed = True
        if src.suffix == ".jsonl":
            before, after = jsonl_parse_failures(text), jsonl_parse_failures(scrubbed)
            if after > before:
                print(f"  JSONL BROKEN by scrub: {dest} ({before}->{after})")
                failed = True

        target = out_root / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(scrubbed)
        manifest.append(
            {
                "dest": dest,
                "src_sha256_16": sha256(text),
                "out_sha256_16": sha256(scrubbed),
                "bytes": len(scrubbed),
                "replacements": counts,
            }
        )

    numbered_sizes = {k: len(v) for k, v in numbered_map.items()}
    (out_root / "MANIFEST.json").write_text(
        json.dumps(
            {"files": manifest, "numbered_unique_values": numbered_sizes},
            indent=1,
        )
    )

    total = {}
    for m in manifest:
        for cls, n in m["replacements"].items():
            total[cls] = total.get(cls, 0) + n
    print(f"\nScrubbed {len(manifest)} files -> {out_root}")
    for cls in sorted(total):
        print(f"  {total[cls]:5d}  {cls}")
    print(f"  numbered maps: {numbered_sizes}")

    if all_detect:
        print("\nDETECTOR HITS (need human review, values not shown):")
        for cls, where, pos in all_detect:
            print(f"  {cls} @ {where}:{pos}")
        return 3
    if failed:
        return 2
    return 0
