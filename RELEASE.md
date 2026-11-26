# Release record: loglearner-minimal 0.1.13

- Artifact: `dist/loglearner-minimal-0.1.13-20261126.tar.gz`, sha256 `9ddb7ece309dc8a9899937b061d6a04e0bbaec3b33f212c6ba38d6fcef460b99`, 72 members. Reproducible: the tarball is packed with the export builder's rules (sorted members, member mtime 0, mode 0755 for `*.sh` and `scripts/hooks/`, gzip level 9) and a gzip header mtime of 0. The same packing of the 0.1.12 tree gives that release's sha256 `c8c208b17530b9c91460586717ba46e1a9e9174b8297fc2621538da4eaf4b441` again.
- What changed since 0.1.12:
  - The dashboard no longer reads "database is locked" while the indexer rebuilds the index. This fix came from the aws-do-agent harness, which found that its periodic refresh held the index's exclusive lock for most of each 300 s cycle, so the tile read Down.
    - `dashboard/indexer.py` opens the index with a 30 s busy timeout and switches the file to WAL mode, so read-only readers read beside its writes.
    - The new flag `--skip-similarity-when-unchanged` keeps the similarity pairs and edges when no session or bank changed. It works with a `last_index_sig` meta key.
    - `dashboard/app.py` waits up to 30 s on its index connections.
    - `/api/tile.json` serves the last good counts with `stale: true` and a `note` when a read hits a transient lock. The status stays `ok`.
    - A client that disconnects early is logged in one line instead of a traceback.
  - `loglearner/arms.py` and `loglearner/steps.py` mark their four sha1 id hashes `usedforsecurity=False`. They are stable ids, not security hashes, and this lets the runtime run where FIPS mode restricts sha1.
  - New test: `tests/test_index_lock.py`. Three of its tests fail on 0.1.12; a fourth checks that a lock with no earlier good read still turns the tile red.
  - New file: `THIRD-PARTY-LICENSES`, covering the runtime's dependencies and their licenses. The runtime is stdlib-only; Cytoscape.js (MIT) is loaded by the browser; hindsight_api (MIT) is optional.
  - Compatibility: no existing CLI flag, env name or path changed. One optional indexer flag was added; without it the indexer behaves as before.
- The files at the top of this repository are that tarball's 72 members, unchanged, except README.md, which has the public-release note at the top (original README.md sha256 `7a2ffcf64ff43905a890e7894494a0ce9f69df29af6f9246c79c3ff85d66912c`).
- License: MIT (LICENSE).
