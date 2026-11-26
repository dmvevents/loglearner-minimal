"""Fail-closed export gate (KR5.2): every byte that leaves via the dashboard
/export endpoint routes through the scrub engine first.

Fail-closed means export is REFUSED (ExportBlocked) when:
  * no scrub config exists (env LOGLEARNER_SCRUB_CONFIG or state/scrub-patterns.json
    — the committed example config is deliberately NOT a fallback),
  * any detector fires (possible literal secret — reported as class+file+offset,
    the candidate value itself is never echoed),
  * any replacement/numbered pattern still matches the scrubbed output (residual),
  * a matched file is on the config's file_denylist (silently excluded, noted
    in the manifest).

Success returns a deterministic .tar.gz of scrubbed artifacts + MANIFEST.json.
"""

import io
import json
import os
import tarfile
from pathlib import Path

from loglearner import scrub_core

REPO = Path(__file__).resolve().parent.parent

# Shareable mined artifacts only — never raw session transcripts.
EXPORT_GLOBS = (
    "output_llm/**/instincts.json",
    "output_llm/**/examples.jsonl",
    "output_llm/**/report.md",
    "output/isolates/*.json",
)


class ExportBlocked(Exception):
    """Raised whenever the gate refuses to let bytes out. str() is safe to
    show a user: it carries classes/paths/offsets, never candidate values."""


def find_config(root=None):
    env = os.environ.get("LOGLEARNER_SCRUB_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    p = Path(root or REPO) / "state" / "scrub-patterns.json"
    return p if p.is_file() else None


def collect_files(root=None, globs=EXPORT_GLOBS):
    """relpath -> text for every exportable artifact under root."""
    root = Path(root or REPO)
    files = {}
    for pattern in globs:
        for p in sorted(root.glob(pattern)):
            if p.is_file():
                files[str(p.relative_to(root))] = p.read_text(errors="replace")
    return files


def scrub_bundle(files, cfg):
    """Scrub {relpath: text}; returns (scrubbed_files, manifest).
    Raises ExportBlocked on detector hits or residuals."""
    files = {
        rel: text
        for rel, text in files.items()
        if Path(rel).name not in cfg["file_denylist"]
    }
    numbered_map = scrub_core.collect_numbered(
        cfg, list(files.values()) + list(files)
    )

    detector_hits = []
    for rel, text in files.items():
        detector_hits.extend(scrub_core.detect(cfg, text, rel))
    if detector_hits:
        lines = [f"{cls} @ {where}:{pos}" for cls, where, pos in detector_hits]
        raise ExportBlocked(
            "detector hit(s) — possible literal secret, human review required "
            "(values not shown): " + "; ".join(lines)
        )

    out, manifest = {}, []
    for rel, text in files.items():
        dest = scrub_core.scrub_dest(cfg, rel, numbered_map)
        scrubbed, counts = scrub_core.scrub_text(cfg, text, numbered_map)
        residual = scrub_core.verify_clean(cfg, scrubbed) + scrub_core.verify_clean(
            cfg, dest
        )
        if residual:
            raise ExportBlocked(
                f"residual pattern match after scrub in {dest}: "
                + ", ".join(f"{cls} x{n}" for cls, n in residual)
            )
        if rel.endswith(".jsonl") and scrub_core.jsonl_parse_failures(
            scrubbed
        ) > scrub_core.jsonl_parse_failures(text):
            raise ExportBlocked(f"scrub broke JSONL parsability of {dest}")
        out[dest] = scrubbed
        manifest.append(
            {
                "dest": dest,
                "src_sha256_16": scrub_core.sha256(text),
                "out_sha256_16": scrub_core.sha256(scrubbed),
                "bytes": len(scrubbed.encode("utf-8")),  # bytes on disk, not characters
                "replacements": counts,
            }
        )
    return out, manifest


def scrubbed_tarball(root=None, cfg_path=None):
    """The single entry point the dashboard uses. Returns (tar_gz_bytes,
    manifest); raises ExportBlocked (fail-closed) on any gate condition."""
    root = Path(root or REPO)
    cfg_path = cfg_path or find_config(root)
    if cfg_path is None:
        raise ExportBlocked(
            "no scrub config — export is disabled until state/scrub-patterns.json "
            "exists (or LOGLEARNER_SCRUB_CONFIG points at one). The committed "
            "example config is not used as a fallback by design."
        )
    cfg = scrub_core.load_config(cfg_path)
    files = collect_files(root)
    if not files:
        raise ExportBlocked("nothing to export — no mined artifacts found")

    scrubbed, manifest = scrub_bundle(files, cfg)
    scrubbed["MANIFEST.json"] = json.dumps({"files": manifest}, indent=1)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dest in sorted(scrubbed):
            data = scrubbed[dest].encode()
            info = tarfile.TarInfo(name=f"loglearner-export/{dest}")
            info.size = len(data)
            info.mtime = 0  # deterministic archive
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue(), manifest
