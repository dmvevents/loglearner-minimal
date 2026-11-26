#!/usr/bin/env bash
# loglearner-pull.sh — OPTIONAL pull-on-start of a knowledge pack from S3, then idempotent ingest.
# OFF unless LOGLEARNER_S3_BUCKET is set ("repo first, S3 later"). Never writes to S3. Fails closed:
# a sha256 mismatch leaves the existing bank untouched and exits non-zero.
#
# Env: LOGLEARNER_S3_BUCKET (required to do anything)   LOGLEARNER_PACK (default aws-default)
#      LOGLEARNER_PACK_VERSION (default latest)          LOGLEARNER_BANK (default ~/.loglearner/banks/global/instincts.jsonl)
#      LOGLEARNER_S3_PREFIX (default loglearner/packs)   LOGLEARNER_REPO (dir containing loglearner/; default: this script's parent/..)
set -uo pipefail
[ -n "${LOGLEARNER_S3_BUCKET:-}" ] || { echo "loglearner-pull: LOGLEARNER_S3_BUCKET unset — pull disabled (repo-first mode)"; exit 0; }
command -v aws >/dev/null || { echo "loglearner-pull: aws cli not found" >&2; exit 1; }
PACK="${LOGLEARNER_PACK:-aws-default}"; VER="${LOGLEARNER_PACK_VERSION:-latest}"
BANK="${LOGLEARNER_BANK:-$HOME/.loglearner/banks/global/instincts.jsonl}"
PFX="s3://${LOGLEARNER_S3_BUCKET}/${LOGLEARNER_S3_PREFIX:-loglearner/packs}/${PACK}"
REPO="${LOGLEARNER_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
if [ "$VER" = latest ]; then
  aws s3 cp --only-show-errors "$PFX/latest.json" "$T/latest.json" || { echo "loglearner-pull: no latest.json" >&2; exit 1; }
  VER=$(python3 -c "import json;print(json.load(open('$T/latest.json'))['version'])")
fi
aws s3 cp --only-show-errors "$PFX/$VER/manifest.json" "$T/manifest.json" || { echo "loglearner-pull: no manifest for $VER" >&2; exit 1; }
WANT=$(python3 -c "import json;m=json.load(open('$T/manifest.json'));print(m.get('sha256') or [f['sha256'] for f in m.get('files',[]) if f.get('path','').endswith('.jsonl')][0])")
INSTALLED="$(dirname "$BANK")/.installed-packs.json"
if [ -f "$INSTALLED" ] && grep -q "\"sha256\": \"$WANT\"" "$INSTALLED"; then echo "loglearner-pull: $PACK@$VER already installed ($WANT) — no-op"; exit 0; fi
aws s3 cp --only-show-errors "$PFX/$VER/instincts.jsonl" "$T/instincts.jsonl" || { echo "loglearner-pull: fetch failed" >&2; exit 1; }
GOT=$(sha256sum "$T/instincts.jsonl" | cut -d' ' -f1)
[ "$GOT" = "$WANT" ] || { echo "loglearner-pull: sha256 mismatch (manifest $WANT, got $GOT) — bank untouched" >&2; exit 1; }
cd "$REPO" && python3 -m loglearner.pack ingest --pack "$T/instincts.jsonl" --bank "$BANK" --name "$PACK" --version "$VER" --manifest "$T/manifest.json"
