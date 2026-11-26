# Release record: loglearner-minimal 0.1.12

- Artifact: `dist/loglearner-minimal-0.1.12-20261126.tar.gz`, sha256 `c8c208b17530b9c91460586717ba46e1a9e9174b8297fc2621538da4eaf4b441`, 70 members. Reproducible: the tarball is packed with the export builder's rules (sorted members, member mtime 0, mode 0755 for `*.sh` and `scripts/hooks/`, gzip level 9) and a gzip header mtime of 0.
- What changed since 0.1.11 (sha256 `fdb00b875ea9b36df05682d83a4992a839b8c7270ac60f10e9636ad64ebe2a43`): one registry entry in `loglearner/registry/labeling_profiles/hpc-derived.json` that still carried an internal lane label is emptied (`[]`), the same way the build already emptied another entry in that file. No code, CLI, env name or path changed.
- The files at the top of this repository are that tarball's 70 members, unchanged, except README.md, which has the public-release note at the top (original README.md sha256 `7a2ffcf64ff43905a890e7894494a0ce9f69df29af6f9246c79c3ff85d66912c`).
- License: MIT (LICENSE).
