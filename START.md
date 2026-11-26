# Log Learner minimal runtime — start commands

Requirements: python3 >= 3.10 (stdlib only), sqlite3 with FTS5 (stock Ubuntu 22.04 python3 has it), no GPU.
Run as any user; the runtime writes only under LOGLEARNER_* paths (defaults: ~/.loglearner/*).

    export LOGLEARNER_HOME=~/.loglearner
    export LOGLEARNER_DB=$LOGLEARNER_HOME/index.db              # dashboard index
    export LOGLEARNER_BANKS_DIR=$LOGLEARNER_HOME/banks           # knowledge banks (*.jsonl), global + per-project
    export INSTINCTS_JSON=$LOGLEARNER_HOME/banks/global/instincts.jsonl   # store the hook serves
    export FLEET_LEARNINGS_DIR=$LOGLEARNER_HOME/banks            # second store (newest-first content pass)
    export LOGLEARNER_UCB_DIR=$LOGLEARNER_HOME/output            # bandit + exposure ledgers
    export LOGLEARNER_HINDSIGHT_DISABLE=1                        # unless a Hindsight is reachable on 127.0.0.1:8888
    export HINDSIGHT_API=http://127.0.0.1:8888                   # where THIS server reaches Hindsight (loopback)
    export LOGLEARNER_HINDSIGHT_LINK_BASE=/memory-api            # where the BROWSER reaches it, e.g. a proxy path, when only
                                                                 # the dashboard port is published (default: HINDSIGHT_API)
    export LOGLEARNER_SCRUB_CONFIG=$PWD/config/scrub-patterns.json   # REQUIRED for /export; copy the example and fill REAL values

    # 1. first start: ingest the curated default pack (idempotent; re-running is a no-op)
    python3 -m loglearner.pack ingest --pack knowledge/defaults/aws-basics.jsonl \
        --bank $LOGLEARNER_BANKS_DIR/global/instincts.jsonl --name aws-basics --version v1

    # 2. index the agent's own Claude Code sessions (~/.claude/projects) — seconds for a fresh user
    python3 dashboard/indexer.py --db $LOGLEARNER_DB

    # 3. serve (loopback only, by design)
    python3 dashboard/app.py --port 7777
    curl -s http://127.0.0.1:7777/api/tile.json | python3 -m json.tool     # status must be "ok"

    # 4. (optional) deliver instincts into Claude Code sessions — PreToolUse hook on Edit, Write AND Bash. The harness adds
    #    this entry to settings.json once (install_pretooluse_hook.py only updates an existing entry's command/matcher):
    #    {"matcher":"Edit|Write|Bash","hooks":[{"type":"command","command":"python3 /abs/path/scripts/hooks/pretooluse-instinct-context.py","timeout":10}]}
    python3 scripts/hooks/probe_hook.py "python3 $PWD/scripts/hooks/pretooluse-instinct-context.py" /x/handler.py Write "boto3 client"
    echo '{"session_id":"probe","tool_name":"Bash","tool_input":{"command":"aws ec2 describe-capacity-block-offerings"}}' \
        | python3 scripts/hooks/pretooluse-instinct-context.py      # Bash routing: one JSON object when a rule matches, else silence

    # 5. recall and bank from a Bash call — what the agent itself runs (AGENT-USAGE.md has the full contract)
    python3 -m loglearner.cli recall "capacity block availability zone"          # exit 0 on a hit, 1 on none; --json for records
    python3 -m loglearner.cli bank --json '{"rule":"...","evidence":"...","confidence":"high","source":"...","provenance":"...","status":"positive","category":"...","tags":["docker"]}'

    # 6. (optional, OFF by default) pull a pack from S3 on start — does nothing unless LOGLEARNER_S3_BUCKET is set
    scripts/loglearner-pull.sh

Hook output: each delivered record renders `## <id> [confidence: …]`, the rule, `Status:`, `Provenance:`, `Scope:` (the
record's version_scope — versions, instance types, conditions — whenever the bank carries one), `Evidence:`, `Source:`.
Pack ingest keeps version_scope / as_of / source_type / model_likely_knows from K1/K2-format records.

Tile contract: GET /api/tile.json -> {"schema":"loglearner-tile/v1","status":"ok|degraded","log_learner":{ok,...},
"meta_learner":{ok,...,"bandit":{...}},"links":{...},"errors":[...]} — always HTTP 200; a missing source sets ok:false
and status "degraded" so the tile renders RED, never blank.
