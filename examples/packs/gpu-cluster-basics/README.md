# gpu-cluster-basics: an example LogLearner knowledge pack

A small, public example of a LogLearner knowledge pack. It holds 21 generic troubleshooting records for GPU clusters:
NCCL, AWS EFA, Kubernetes, NVIDIA GPU tooling and PyTorch. Each record was written for this example from a public
documentation page, and that page is the record's `source`. `SOURCES.md` lists them all. The pack contains no records
from any team, session or project.

Files:
- `gpu-cluster-basics.jsonl`: the records, in the pack loader's simple schema
  (`id`, `title`, `text`, `category`, `tags`, `confidence`, `source`).
- `gpu-cluster-basics.manifest.json`: its name, version and sha256. The loader refuses the pack if the sha256 does
  not match.
- `SOURCES.md`: the documentation pages the records are written from.
- `MANIFEST.sha256`: the sha256 of every file in this directory.

Load it into a bank with the runtime's pack loader, run from the repository root:

```
python3 -m loglearner.pack verify --pack examples/packs/gpu-cluster-basics/gpu-cluster-basics.jsonl \
    --manifest examples/packs/gpu-cluster-basics/gpu-cluster-basics.manifest.json
python3 -m loglearner.pack ingest --pack examples/packs/gpu-cluster-basics/gpu-cluster-basics.jsonl \
    --bank ~/.loglearner/banks/global/instincts.jsonl --name gpu-cluster-basics --version 0.1.0 \
    --manifest examples/packs/gpu-cluster-basics/gpu-cluster-basics.manifest.json
```

Ingesting the same bytes again changes nothing. To write a pack of your own, copy this layout and keep one claim per
record. Give each record the source it comes from, and record the new file's sha256 in the manifest.

The records are short summaries of the cited documentation and do not replace it. Check the source page for the
version you run.
