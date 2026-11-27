> **Public minimal runtime of LogLearner; upstream development is private.** This repository is the gated minimal-runtime export (version 0.1.13), file-for-file identical to `dist/loglearner-minimal-0.1.13-20261126.tar.gz` except for this note. See RELEASE.md.
>
> examples/ holds an example knowledge pack, published as its own release asset; it is not part of the runtime tarball.

# Log Learner — minimal runtime (vendored)

Scrubbed, stdlib-only runtime: dashboard + indexer, knowledge banks + idempotent pack ingest, UCB bandit ledgers, lifecycle, and the PreToolUse delivery hook. See START.md. Built by scripts/ship_minimal_runtime.py; see manifest.json and SCRUB-REPORT.md for provenance and the gates this build passed. Licensed under the MIT License (LICENSE, shipped verbatim).
