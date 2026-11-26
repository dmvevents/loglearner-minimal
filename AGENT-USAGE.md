# Log Learner for agents — recall, bank, and what the hook does

Copy this file (or the parts you want) into your agent's CLAUDE.md. Everything here is stdlib Python, loopback only,
and read-only on your sessions. The runtime never changes a tool call; it only adds banked knowledge to the context.

## 1. The PreToolUse hook (automatic)

When the hook entry is registered (matcher `Edit|Write|Bash`), every Edit, Write and Bash call is routed before it runs:

- **files** route on path tokens and extension (`Dockerfile`, `*.tf`, `*.yaml`, `*.sh`, `*.cu`, ...), never substrings;
- **Bash** routes on the program names in the command: `docker|buildx|podman` → containers, `kubectl|eksctl|helm|kustomize`
  → eks-k8s, `aws ec2|eks|iam|bedrock|s3|ssm|fsx|cloudwatch` → that service, `fi_info|nccl-tests|all_reduce_perf|efa`
  → efa-networking, `srun|sbatch|salloc` → slurm, `git|gh` → git, `pip|uv|npm|apt` → deps,
  `vllm|sglang|trtllm|dynamo|aiperf` → serving;
- **agent configuration** routes to the agent-engineering class: editing `settings.json`, hooks, `SKILL.md`, `AGENTS.md`,
  `CLAUDE.md` or a codex config, and Bash calls to `claude`, `codex` or `tmux`, deliver the records about skills, hooks,
  permission modes, sandboxing and lane operating rules at the moment the agent changes itself;
- the text being written (or the command itself) contributes content tokens (`capacity_block`, `--instance-type`).

Matching rules arrive as `Banked learnings matched to this edit|command — N of M shown (...)`, each with its id,
confidence, Status (positive = practice, negative = BANLIST / dead end), Provenance, Evidence and Source. Weigh each by
its own lines: a negative entry is a warning, not a recipe. The hook is bounded (about 900 tokens), remembers what it
already showed this session, always exits 0 within its timeout, and is quiet when nothing matches. Every delivery is
written to the exposure ledger (`$LOGLEARNER_UCB_DIR/loglearner_exposures.jsonl`); that is delivery, not reward.

## 2. Recall before the risky step (deliberate)

Run `recall` yourself before an action that is expensive to get wrong, and read the result before proceeding:

    python3 -m loglearner.cli recall "docker build NGC base EFA installer"        # before a container build
    python3 -m loglearner.cli recall "karpenter nodepool gpu taint" --category eks-k8s-gpu   # before a cluster change
    python3 -m loglearner.cli recall "iam policy least privilege" --status negative          # before an IAM edit
    python3 -m loglearner.cli recall "capacity block availability zone" --k 3 --json

Exit code 0 means at least one banked rule matched (plain text, one record per block); 1 means none. `--json` returns
the raw records. `--session <id>` also records the exposure, if you want the delivery counted like the hook's.

Good moments to recall: before `docker build`/`push`, before `kubectl apply`, `helm upgrade`, `eksctl create`, before
any `aws iam`/`ec2 purchase`/`capacity-block` call, before `sbatch` on a shared cluster, and before publishing a
benchmark number.

## 3. Bank a verified lesson (the only write path)

Bank only what you verified yourself, one rule per record, with the evidence that proves it. The CLI is the sanctioned
write path: a schema gate, a recall echo of the nearest existing records, duplicate rejection, a stable id, an append.

    python3 -m loglearner.cli bank --json '{
      "rule": "Run the container smoke against a fresh image tag before pushing; never retag a stale local image.",
      "evidence": "2026-10-13 build: retagged image shipped a stale wheel; fresh tag reproduced the fix",
      "confidence": "high",
      "source": "team practice",
      "provenance": "verified by <agent/session>: command + output in the session log",
      "status": "positive",
      "category": "containers-and-builds",
      "tags": ["docker", "ecr"]
    }'

Required: `rule`, `evidence`, `confidence` (high|med|low), `source`, `provenance`, `status` (positive|negative).
Optional: `title` (<= 80 chars), `category`, `tags` (lowercase routing words so the hook finds it), `cross_ref`,
`corrects` (ids this record supersedes; the hook then shows the pair together), `version_scope`, `as_of`.
Exit 0 prints `{"banked": "kb-0007", ...}`; 2 means the schema gate refused (the message lists every problem);
3 means a duplicate or near-duplicate exists (bank the correction with `corrects` instead, or `--force` for a genuinely
different rule). Never bank secrets, account ids, hostnames, customer or people names; write the rule as if the bank
were public. The Meta Learner tile counts the new record immediately; the hook serves it on the next matching call.

## 4. Health

`GET http://127.0.0.1:7777/api/tile.json` must report `"status": "ok"`; `degraded` names the missing source.
`/knowledge` lists the installed banks and packs with sample rules; `/instincts` and `/graph` show the same banks as an
instinct library; `/learnings` separates bank polarity from measured rewards.
