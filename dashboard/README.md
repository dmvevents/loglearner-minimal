# LogLearner Dashboard (v0)

Read-only local UI over the Claude Code session corpus: browse projects/sessions,
heuristic summaries, gap-evidence overlays on transcripts, instinct library,
knowledge graph, Hindsight panel.

```bash
python3 dashboard/indexer.py        # build/refresh index (incremental; ~14 s cold for 21 GB)
python3 dashboard/app.py            # serve http://127.0.0.1:7777  (loopback ONLY)
# remote viewing: ssh -L 7777:localhost:7777 <host>
```

Views: `/` corpus overview · `/project/<name>` session cards · `/session/<proj>/<id>`
windowed transcript (+`?start=N`) with gap banners/highlights · `/instincts` filterable
library · `/graph` instinct↔session↔project graph (Cytoscape) · `/hindsight` memory bank
panel (proxies `127.0.0.1:8888`).

Guardrails (deliberate): binds 127.0.0.1 only, GET-only, session JSONL never opened for
write, stdlib-only (no deps). Graph page pulls Cytoscape from unpkg; offline it degrades
to the instinct table. See `OKRS.md` for targets/status, `ARCHITECTURE.md` for decisions,
GitHub issues E1–E6 for the roadmap.
