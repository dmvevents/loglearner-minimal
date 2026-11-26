"""D1 — the PreToolUse instinct hook must DELIVER context, not just print it.

Two defects it guards (measured 2026-09-27, reproduced independently by Codex):
1. the previous hook routed on $CLAUDE_FILE_PATH, which Claude Code never sets -> 0 instincts;
2. it printed plain text, and for PreToolUse plain stdout on exit 0 goes to the debug log, not the
   model's context (code.claude.com/docs/en/hooks). Context requires
   hookSpecificOutput.additionalContext.

These tests run the hook exactly as Claude Code does: JSON on stdin, CLAUDE_FILE_PATH unset.
Exposure writes are redirected to tmp_path so the real ledger is never touched.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "pretooluse-instinct-context.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CU = "/x/deep_ep/csrc/kernels/internode_ll.cu"
# Hermetic by default: every store the hook reads points at fixtures, never at this host's fleet stores,
# so the suite passes identically in a fresh container (shipped tree) and on the fleet host.
FIXTURE_ENV = {"INSTINCTS_JSON": str(FIXTURES / "instincts-fixture.json"),
               "FLEET_LEARNINGS_DIR": str(FIXTURES / "fleet-bank"),
               "LOGLEARNER_CAUTIONS_SCRIPT": str(FIXTURES / "cautions.sh"),
               "LOGLEARNER_KNOWLEDGE_ROOT": str(FIXTURES / "no-reference")}


def run(tmp_path, file_path, content="int x = 1;", tool="Write", session="t-sess", **env_extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_FILE_PATH", "INSTINCTS_JSON", "FLEET_LEARNINGS_DIR", "LOGLEARNER_CAUTIONS_SCRIPT", "LOGLEARNER_KNOWLEDGE_ROOT")}
    env.update(FIXTURE_ENV)
    env["LOGLEARNER_UCB_DIR"] = str(tmp_path)
    env.update({k: str(v) for k, v in env_extra.items()})
    payload = {"session_id": session, "cwd": "/srv/x", "hook_event_name": "PreToolUse",
               "transcript_path": f"/h/.claude/projects/-proj/{session}.jsonl",
               "tool_name": tool, "tool_input": {"file_path": file_path, "content": content}}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, f"hook must never block the tool: {r.returncode} {r.stderr[:400]}"
    out = r.stdout.strip()
    if not out:
        return None, ""
    d = json.loads(out)  # must be exactly one JSON object, else this raises
    hso = d["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    return d, hso.get("additionalContext", "")


def ids(ctx):
    return [l.split()[1] for l in ctx.splitlines() if l.startswith("## ")]


def test_delivers_additional_context_not_plain_stdout(tmp_path):
    """The load-bearing fix: a file-routed edit yields hookSpecificOutput.additionalContext with rules."""
    d, ctx = run(tmp_path, CU)
    assert d is not None and ids(ctx), "no instincts delivered for a .cu edit"
    assert "manifest:" in ctx


def test_env_var_is_not_required(tmp_path):
    """CLAUDE_FILE_PATH is stripped in run(); delivery must come from stdin tool_input.file_path."""
    _d, ctx = run(tmp_path, CU)
    assert ids(ctx)


def test_cuda_and_header_forms_all_route(tmp_path):
    """The forms the kernel lane edits — .cu/.cuh/.hpp/.cpp/.py — must all deliver (the old table missed headers)."""
    for i, f in enumerate(["/x/k.cu", "/x/utils.cuh", "/x/config.hpp", "/x/proxy_efa.cpp", "/x/buffer.py"]):
        _d, ctx = run(tmp_path / f"s{i}", f, session=f"s{i}")
        assert ids(ctx), f"no delivery for {f}"


def test_content_tokens_retrieve_task_relevant_rules(tmp_path):
    """Filename alone is not task context: a num_sms edit must pull num_sms knowledge."""
    _d, ctx = run(tmp_path, CU, content="constexpr int num_sms = 8; auto s = qp_channel_slots(num_sms);")
    man = json.loads(ctx.split("manifest: ")[1])
    assert "num_sms" in man["content_tokens"]
    assert man["task_context_ids"], "content-token pass delivered nothing"
    assert any("num_sms" in l for l in ctx.splitlines()), "no num_sms rule in the delivered text"
    assert "fxb-001" in ids(ctx), "fleet-bank record matching the token was not delivered"


def test_status_and_provenance_pass_through_from_both_stores(tmp_path):
    """Review item 2: the reader must see each record's own Status/Provenance, and the header must
    not call unmeasured designs 'measured runs'."""
    env = _stores(tmp_path, n_instincts=2, n_fleet=1)
    inst = json.loads((tmp_path / "instincts.json").read_text())
    inst[0]["provenance"] = {"origin": "code_analysis", "tested": False}
    (tmp_path / "instincts.json").write_text(json.dumps(inst))
    _d, ctx = run(tmp_path, "/x/notes.txt", content="zeta_knob = 8", **env)
    assert "Status: active" in ctx and "Status: negative" in ctx
    assert 'Provenance: {"origin":"code_analysis","tested":false}' in ctx
    assert "Derived from measured runs" not in ctx and "Evidence strength varies" in ctx


def test_caution_routing_preserved(tmp_path):
    """The content scan (wired 2026-09-22) must still fire — the fix must not regress it."""
    _d, ctx = run(tmp_path, "/x/notes.md", content="the root cause identified and resolved")
    assert "Caution rules matched" in ctx


def test_context_is_bounded_and_rules_are_whole(tmp_path):
    # the kernel/cuda route matches 11 padded fixture rules, so a 1200-char budget must drop some whole
    _d, ctx = run(tmp_path, "/x/k.cu", INSTINCT_BUDGET_CHARS=1200, CAUTION_BUDGET_CHARS=1)
    body = ctx.split("manifest: ")[0]
    assert len(body) < 3000
    man = json.loads(ctx.split("manifest: ")[1])
    assert man["dropped_budget"], "budget was not exercised"
    for rid in man["included"]:                      # every included rule is whole
        assert f"## {rid} [" in ctx and "Source:" in ctx
    assert not set(man["included"]) & set(man["dropped_budget"])


def test_exposure_recorded_for_delivered_rules_only(tmp_path):
    _d, ctx = run(tmp_path, CU)
    man = json.loads(ctx.split("manifest: ")[1])
    rows = [json.loads(x) for x in (tmp_path / "loglearner_exposures.jsonl").read_text().splitlines()]
    assert man["exposure_rows"] == len(man["included"]) == len(rows)
    assert [r["instinct_id"] for r in rows] == man["included"]
    assert all(r["session_id"] == "t-sess" and r["decision_ref"].startswith("Write:") for r in rows)


def test_delivery_cache_stops_repeating_the_same_rule(tmp_path):
    _d, c1 = run(tmp_path, CU)
    _d, c2 = run(tmp_path, CU)
    assert ids(c1) and not (set(ids(c1)) & set(ids(c2))), "same rules re-injected on the next edit"


def test_expired_cache_allows_redelivery(tmp_path):
    _d, c1 = run(tmp_path, CU)
    _d, c2 = run(tmp_path, CU, DELIVERY_TTL_S=0)
    assert set(ids(c1)) & set(ids(c2)), "TTL=0 should allow re-delivery"


def test_unknown_extension_delivers_nothing(tmp_path):
    d, _ctx = run(tmp_path, "/x/data.bin", content="")
    assert d is None, "no route + no content must produce empty stdout, not a JSON object"


def test_missing_stores_do_not_block(tmp_path):
    """A knowledge store that is gone must degrade to silence, never a non-zero exit."""
    d, _ctx = run(tmp_path, CU, INSTINCTS_JSON="/nonexistent/instincts.json",
                  FLEET_LEARNINGS_DIR="/nonexistent/learnings")
    assert d is None or "Caution" in _ctx


def test_exposure_can_be_disabled(tmp_path):
    _d, ctx = run(tmp_path, CU, HOOK_DISABLE_EXPOSURE=1)
    assert json.loads(ctx.split("manifest: ")[1])["exposure_rows"] == 0
    assert not (tmp_path / "loglearner_exposures.jsonl").exists()


# ---------------------------------------------------------------------------------------------
# Rotation (review 2026-09-27, item 3): a recently delivered rule must ROTATE OUT and the next-ranked
# match must rotate IN. The first implementation truncated each store to its limit BEFORE the TTL
# suppression, so a match ranked below the limit could never be delivered. Isolated four-rule
# fixture, three-rule limit, both stores.
# ---------------------------------------------------------------------------------------------
def _stores(tmp_path, n_instincts=4, n_fleet=0):
    inst = [{"id": f"FX-{i}", "rule": f"fixture rule {i} about zeta_knob tuning", "confidence": 9 - i,
             "evidence": "fixture", "source": "fixture", "status": "active"} for i in range(n_instincts)]
    ipath = tmp_path / "instincts.json"; ipath.write_text(json.dumps(inst))
    fdir = tmp_path / "fleet"; fdir.mkdir()
    if n_fleet:
        rows = [{"id": f"FL-{i}", "rule": f"fleet fixture {i} about zeta_knob budget", "confidence": "high",
                 "evidence": "fixture", "source": "fixture", "status": "negative"} for i in range(n_fleet)]
        (fdir / "fx-learnings.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return {"INSTINCTS_JSON": str(ipath), "FLEET_LEARNINGS_DIR": str(fdir)}


def test_rotation_route_store_four_rules_limit_three(tmp_path):
    """Route pass: 4 matching rules, INSTINCT_LIMIT=3 -> edit 1 delivers 3, edit 2 delivers the 4th."""
    env = _stores(tmp_path, n_instincts=4)
    # route 'kernel cuda' won't match the fixture; drive selection via the content token instead? No —
    # keep the route pass isolated: route to a fixture keyword by naming the file so 'kernel' matches.
    env2 = dict(env)
    inst = json.loads((tmp_path / "instincts.json").read_text())
    for r in inst:
        r["rule"] = r["rule"].replace("zeta_knob tuning", "kernel tuning")   # matches route kw 'kernel'
    (tmp_path / "instincts.json").write_text(json.dumps(inst))
    _d, c1 = run(tmp_path, "/x/k.cu", content="", INSTINCT_LIMIT=3, CONTENT_TOKENS=0, **env2)
    _d, c2 = run(tmp_path, "/x/k.cu", content="", INSTINCT_LIMIT=3, CONTENT_TOKENS=0, **env2)
    assert sorted(ids(c1)) == ["FX-0", "FX-1", "FX-2"], ids(c1)
    assert ids(c2) == ["FX-3"], f"4th rule did not rotate in: {ids(c2)}"


def test_rotation_fleet_store_four_rules_limit_three(tmp_path):
    """Content-token pass over the fleet bank: 4 matching records, FLEET_LIMIT=3 -> 3 then the 4th."""
    env = _stores(tmp_path, n_instincts=0, n_fleet=4)
    _d, c1 = run(tmp_path, "/x/notes.txt", content="zeta_knob = 8", FLEET_LIMIT=3, **env)
    _d, c2 = run(tmp_path, "/x/notes.txt", content="zeta_knob = 8", FLEET_LIMIT=3, **env)
    assert len(ids(c1)) == 3 and set(ids(c1)) <= {"FL-0", "FL-1", "FL-2", "FL-3"}, ids(c1)
    assert len(ids(c2)) == 1 and ids(c2)[0] not in ids(c1), f"4th fleet record did not rotate in: {ids(c2)}"


def test_rotation_exhausts_then_goes_silent(tmp_path):
    env = _stores(tmp_path, n_instincts=0, n_fleet=4)
    for _ in range(2):
        run(tmp_path, "/x/notes.txt", content="zeta_knob = 8", FLEET_LIMIT=3, **env)
    d, c3 = run(tmp_path, "/x/notes.txt", content="zeta_knob = 8", FLEET_LIMIT=3, **env)
    assert not ids(c3), f"nothing new should remain, got {ids(c3)}"


# ---------------------------------------------------------------------------------------------
# Correction-aware retrieval (post-resume review 2026-09-27): a selected record that has an
# EXPLICITLY linked correction must never be emitted alone as current evidence. Include the
# correction within the existing budget, or omit the older record if the pair cannot fit.
# The correction may lack the query tokens entirely (the real case: the correction uses C++ template
# names while the edit says num_sms/num_qps). Links come only from explicit markers — a rule that
# begins "CORRECTION ... to <id>" or meta.corrects — never from generic meta.cross_ref.
# ---------------------------------------------------------------------------------------------
LONG = " lorem ipsum dolor sit amet consectetur" * 12   # ~470 chars of padding


def _corr_store(tmp_path, *, marker="rule", extra=0, cross_ref_only=False):
    """Fleet bank fixture: T (target, has token), C (correction, NO token), optional others (token)."""
    fdir = tmp_path / "fleet"; fdir.mkdir(exist_ok=True)
    rows = [{"id": "FL-T", "rule": "target record about zeta_knob sizing; budget 8 = ceil(4x4/6)" + LONG,
             "confidence": "high", "evidence": "old", "source": "fx", "status": "positive",
             "provenance": "fx", "meta": {"arm": "a", "reward": 1}}]
    c = {"id": "FL-C", "confidence": "high", "evidence": "source re-read", "source": "fx",
         "status": "negative", "provenance": "fx", "meta": {"arm": "a", "reward": 0}}
    if marker == "rule":
        c["rule"] = "CORRECTION (append-only) to FL-T EVIDENCE: the arithmetic is false; the slot_formula branch gives 4" + LONG
    elif marker == "meta":
        c["rule"] = "The slot_formula branch arithmetic gives 4, not 8" + LONG
        c["meta"]["corrects"] = ["FL-T"]
    if cross_ref_only:  # a RELATED record, not a correction
        c["rule"] = "Related design note without the token" + LONG
        c["meta"]["cross_ref"] = ["FL-T"]
    rows.append(c)
    for i in range(extra):
        rows.append({"id": f"FL-O{i}", "rule": f"other zeta_knob observation {i}" + LONG, "confidence": "med",
                     "evidence": "fx", "source": "fx", "status": "positive", "provenance": "fx",
                     "meta": {"arm": "a", "reward": 1}})
    # newest-first ordering in the hook = later line = newer; put T last so it ranks first
    rows = rows[1:] + rows[:1]
    (fdir / "fx-learnings.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    ipath = tmp_path / "instincts.json"; ipath.write_text("[]")
    return {"INSTINCTS_JSON": str(ipath), "FLEET_LEARNINGS_DIR": str(fdir), "FLEET_LIMIT": 3}


def test_correction_without_query_tokens_rides_along_with_its_target(tmp_path):
    env = _corr_store(tmp_path)
    _d, ctx = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", **env)
    got = ids(ctx)
    assert "FL-T" in got and "FL-C" in got, got
    assert got.index("FL-C") == got.index("FL-T") + 1, "correction must follow its target"
    assert "Corrected-by: FL-C" in ctx
    man = json.loads(ctx.split("manifest: ")[1])
    assert man["corrections_paired"] == {"FL-T": ["FL-C"]}


def test_tight_budget_substitutes_the_correction_for_the_target(tmp_path):
    """Budget fits T alone (and C alone) but not T+C -> T must not be emitted alone; the correction,
    which is self-contained current evidence, is delivered in its place."""
    env = _corr_store(tmp_path, extra=2)
    _d, ctx = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", INSTINCT_BUDGET_CHARS=1300, **env)
    got = ids(ctx)
    assert "FL-T" not in got, f"target emitted without room for its correction: {got}"
    assert "FL-C" in got, f"correction should replace the target: {got}"
    man = json.loads(ctx.split("manifest: ")[1])
    assert "FL-T" in man["omitted_uncorrected"] and man["correction_substituted"] == {"FL-T": ["FL-C"]}


def test_neither_pair_nor_correction_fits_omits_both(tmp_path):
    env = _corr_store(tmp_path)
    fdir = Path(env["FLEET_LEARNINGS_DIR"]); f = fdir / "fx-learnings.jsonl"
    short = {"id": "FL-S", "rule": "short zeta_knob note", "confidence": "med", "evidence": "fx", "source": "fx",
             "status": "positive", "provenance": "fx", "meta": {"arm": "a", "reward": 1}}
    rows = [json.loads(l) for l in f.read_text().splitlines()]
    rows.insert(len(rows) - 1, short)          # ranks right after T
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _d, ctx = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", INSTINCT_BUDGET_CHARS=400, **env)
    got = ids(ctx)
    assert got == ["FL-S"], got
    man = json.loads(ctx.split("manifest: ")[1])
    assert man["omitted_uncorrected"] == ["FL-T"] and man["correction_substituted"] == {}


def test_substituted_target_is_not_reselected_on_the_next_edit(tmp_path):
    """After C stood in for T, the next edit must not re-deliver C (or T) — the pair is handled."""
    env = _corr_store(tmp_path, extra=2)
    _d, c1 = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", INSTINCT_BUDGET_CHARS=1300, **env)
    _d, c2 = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", INSTINCT_BUDGET_CHARS=1300, **env)
    assert "FL-C" in ids(c1)
    assert not ({"FL-T", "FL-C"} & set(ids(c2))), ids(c2)


def test_meta_corrects_field_is_an_explicit_link(tmp_path):
    env = _corr_store(tmp_path, marker="meta")
    _d, ctx = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", **env)
    got = ids(ctx)
    assert "FL-T" in got and "FL-C" in got and got.index("FL-C") == got.index("FL-T") + 1


def test_generic_cross_ref_does_not_invent_a_correction(tmp_path):
    env = _corr_store(tmp_path, cross_ref_only=True)
    _d, ctx = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", **env)
    got = ids(ctx)
    assert "FL-T" in got and "FL-C" not in got, got
    man = json.loads(ctx.split("manifest: ")[1])
    assert man.get("corrections_paired", {}) == {}


def test_correction_is_exempt_from_ttl_when_its_target_is_emitted(tmp_path):
    """C delivered earlier this session must still accompany T on a later edit — T is never alone."""
    env = _corr_store(tmp_path)
    cache = tmp_path / "state" / "hook-delivered"; cache.mkdir(parents=True)
    import time as _t
    (cache / "t-sess.json").write_text(json.dumps({"FL-C": _t.time()}))
    _d, ctx = run(tmp_path, "/x/notes.txt", content="zeta_knob = 64", **env)
    got = ids(ctx)
    assert "FL-T" in got and "FL-C" in got and got.index("FL-C") == got.index("FL-T") + 1, got


def test_correction_alone_is_fine_when_target_not_selected(tmp_path):
    env = _corr_store(tmp_path)
    _d, ctx = run(tmp_path, "/x/notes.txt", content="slot_formula = 4", **env)
    got = ids(ctx)
    assert "FL-C" in got and "FL-T" not in got, got


# ---------------------------------------------------------------------------------------------
# Shipping (aws-do-agent, 2026-10-13): every store path is env-first; a .jsonl bank works as the
# instincts store; a missing cautions script is skipped, never fatal.
# ---------------------------------------------------------------------------------------------
def test_jsonl_bank_works_as_instincts_store_and_cautions_may_be_absent(tmp_path):
    bank = tmp_path / "banks" / "global" / "instincts.jsonl"; bank.parent.mkdir(parents=True)
    bank.write_text(json.dumps({"id": "pk-1", "rule": "Bind local kernel tooling dashboards to loopback only",
                                "status": "positive", "confidence": "high", "evidence": "pack", "source": "pack"}) + "\n")
    _d, ctx = run(tmp_path, "/x/k.cu", content="int x = 1;", INSTINCTS_JSON=str(bank),
                  FLEET_LEARNINGS_DIR=str(tmp_path / "banks"), LOGLEARNER_CAUTIONS_SCRIPT=str(tmp_path / "absent.sh"),
                  LOGLEARNER_KNOWLEDGE_ROOT=str(tmp_path / "no-reference"))
    assert "pk-1" in ids(ctx)
    assert "Caution rules" not in ctx            # cautions script absent -> skipped, not an error


def test_repo_root_env_first(tmp_path):
    """LOGLEARNER_REPO relocates the ledger default (LOGLEARNER_UCB_DIR unset) to <repo>/output."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_FILE_PATH", "LOGLEARNER_UCB_DIR")}
    env.update({"LOGLEARNER_REPO": str(tmp_path / "vendored"), "INSTINCTS_JSON": str(tmp_path / "none.json"),
                "FLEET_LEARNINGS_DIR": str(tmp_path / "nb"), "LOGLEARNER_KNOWLEDGE_ROOT": str(tmp_path / "nc")})
    (tmp_path / "vendored" / "loglearner").mkdir(parents=True)
    (tmp_path / "vendored" / "loglearner" / "__init__.py").write_text("")
    payload = {"session_id": "r", "cwd": "/x", "tool_name": "Write", "transcript_path": "/h/-p/r.jsonl",
               "tool_input": {"file_path": "/x/k.cu", "content": "x"}}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == ""          # no stores -> silence, never an error


# ---------------------------------------------------------------------------------------------
# Default-AWS-user retrieval (aws-do-agent, 2026-10-13): code identifiers must find prose rules
# (capacity_block ~ 'Capacity Block'), and generic infrastructure files must route to cloud vocabulary.
# ---------------------------------------------------------------------------------------------
def test_identifier_underscore_matches_prose_rule(tmp_path):
    bank = tmp_path / "banks" / "global" / "instincts.jsonl"; bank.parent.mkdir(parents=True)
    bank.write_text(json.dumps({"id": "aws-002", "rule": "An EC2 Capacity Block reservation is locked to one Availability Zone.",
                                "status": "positive", "confidence": "high", "evidence": "pack", "source": "pack"}) + "\n")
    _d, ctx = run(tmp_path, "/x/notes.txt", content="capacity_block = true", INSTINCTS_JSON=str(bank), FLEET_LEARNINGS_DIR=str(tmp_path / "none"))
    assert "aws-002" in ids(ctx)


def test_infra_files_route_to_cloud_vocabulary(tmp_path):
    bank = tmp_path / "banks" / "global" / "instincts.jsonl"; bank.parent.mkdir(parents=True)
    bank.write_text(json.dumps({"id": "aws-007", "rule": "An EKS managed node group snapshots the launch template at creation.",
                                "status": "positive", "confidence": "high", "evidence": "pack", "source": "pack"}) + "\n")
    for f in ("/x/infra/nodegroup.tf", "/x/deploy/cluster.yaml", "/x/ci/.github/workflows/build.yml"):
        _d, ctx = run(tmp_path / f.replace("/", "_"), f, content="replicas: 3", INSTINCTS_JSON=str(bank), FLEET_LEARNINGS_DIR=str(tmp_path / "none"))
        assert "aws-007" in ids(ctx), f


# ---- 0.1.1: token-based path routes and Bash command routing (consumer-found defects, 2026-10-13) ----
import importlib.util  # noqa: E402


def _hook_module():
    spec = importlib.util.spec_from_file_location("pretooluse_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_bash(tmp_path, command, session="b-sess", **env_extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_FILE_PATH", "INSTINCTS_JSON", "FLEET_LEARNINGS_DIR", "LOGLEARNER_CAUTIONS_SCRIPT", "LOGLEARNER_KNOWLEDGE_ROOT")}
    env.update(FIXTURE_ENV)
    env["LOGLEARNER_UCB_DIR"] = str(tmp_path)
    env.update({k: str(v) for k, v in env_extra.items()})
    payload = {"session_id": session, "cwd": "/srv/x", "hook_event_name": "PreToolUse",
               "transcript_path": f"/h/.claude/projects/-proj/{session}.jsonl", "tool_name": "Bash",
               "tool_input": {"command": command, "description": "x"}}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, f"hook must never block the tool: {r.returncode} {r.stderr[:400]}"
    out = r.stdout.strip()
    if not out:
        return None, ""
    d = json.loads(out)
    return d, d["hookSpecificOutput"]["additionalContext"]


def manifest_of(ctx):
    line = [l for l in ctx.splitlines() if l.startswith("manifest: ")][-1]
    return json.loads(line[len("manifest: "):])


def test_defaults_dir_is_not_an_efa_route():
    """The exact consumer path: 'defaults' contains the letters efa but is not the token efa."""
    m = _hook_module()
    assert m.route("/knowledge/defaults/Dockerfile") == ["aws", "docker", "git"]
    assert m.route("/x/proxy_efa.cpp") == ["proxy"]
    assert m.route("/x/libfabric_efa.h") == ["fi_write", "libfabric"]
    assert m.route("/x/efa-overlay/transport.md") == ["efa", "rdma"]
    assert m.route("/x/nvshmem_efa/transport.cpp") == ["nvshmem"]
    assert m.route("/x/.github/workflows/ci.yml") == ["aws", "docker", "git", "eks", "kubernetes"]
    assert m.route("/x/eksctl-cluster.yaml") == ["aws", "eks", "ec2", "kubernetes"]
    assert m.route("/x/README") == []


def test_defaults_dockerfile_delivers_docker_rules_through_the_hook(tmp_path):
    _d, ctx = run(tmp_path, "/knowledge/defaults/Dockerfile", content="FROM nvcr.io/nvidia/cuda:13.0")
    assert "FX-B01" in ids(ctx)
    assert manifest_of(ctx)["route"] == ["aws", "docker", "git"]


def test_route_command_tokens():
    m = _hook_module()
    assert m.route_command("docker build -t x . && docker push x") == (["containers"], ["docker", "container", "image", "ecr"])
    labels, kws = m.route_command("aws ec2 describe-capacity-block-offerings --instance-type p5.48xlarge")
    assert labels == ["aws:ec2"] and kws == ["aws", "ec2"]
    labels, _ = m.route_command("kubectl get pods -A | grep efa && fi_info -p efa")
    assert labels == ["eks-k8s", "efa-networking", "shell"]   # grep is a shell-mechanics tool too
    assert m.route_command("srun -N2 ./all_reduce_perf")[0] == ["efa-networking", "slurm"]
    assert m.route_command("ls -la /tmp") == ([], [])
    assert m.route_command('echo "docker build is slow"') == (["shell"], ["quoting", "marker", "echo"])   # quoted word: no containers route
    assert m.route_command("find . -name .git -type d")[0] == ["shell"]                                     # .git argument is not the git program
    assert m.route_command("sudo docker ps") == (["containers"], ["docker", "container", "image", "ecr"])   # wrapper stripped
    assert m.route_command("FOO=1 /usr/bin/git -C /r status")[0] == ["git:status"]                           # assignment + path stripped
    assert m.route_command("") == ([], [])


def test_bash_docker_command_delivers_and_records_exposure(tmp_path):
    d, ctx = run_bash(tmp_path, "docker build --pull -t app:dev .")
    assert d is not None and "FX-B01" in ids(ctx)
    assert "matched to this command" in ctx and "route: bash:containers" in ctx
    man = manifest_of(ctx)
    assert man["route_label"] == "bash:containers" and man["exposure_rows"] >= 1
    rows = [json.loads(l) for l in (tmp_path / "loglearner_exposures.jsonl").read_text().splitlines()]
    assert rows and all(r["decision_ref"] == "Bash:containers" and r["session_id"] == "b-sess" for r in rows)


def test_bash_rule_found_only_by_tag(tmp_path):
    """FX-B02's text has no efa/nccl/rdma word; its tags carry the routing vocabulary and must suffice."""
    _d, ctx = run_bash(tmp_path, "fi_info -p efa -t FI_EP_RDM")
    assert "FX-B02" in ids(ctx)


def test_bash_aws_service_pair_routes(tmp_path):
    _d, ctx = run_bash(tmp_path, "aws ec2 describe-capacity-block-offerings --instance-type p5.48xlarge")
    assert "FX-B03" in ids(ctx) and "route: bash:aws:ec2" in ctx


def test_bash_quiet_when_nothing_matches(tmp_path):
    assert run_bash(tmp_path, "ls -la /tmp && echo done") == (None, "")   # echo routes, but no echo/marker/quoting record exists
    assert run_bash(tmp_path, "") == (None, "")
    assert not (tmp_path / "loglearner_exposures.jsonl").exists()


def test_bash_kebab_flag_becomes_a_content_token(tmp_path):
    _d, ctx = run_bash(tmp_path, "aws ec2 purchase-capacity-block --capacity-block-offering-id cbo-1")
    assert "capacity_block" in manifest_of(ctx)["content_tokens"] or "capacity_block_offering_id" in manifest_of(ctx)["content_tokens"]


# ---- 0.1.5: a record's applicability (version_scope metadata) is delivered with the rule (validator finding) ----
def test_metadata_only_scope_is_delivered_with_the_rule(tmp_path):
    _d, ctx = run_bash(tmp_path, "docker build --pull -t app:dev .")
    blocks = {b.split("\n", 1)[0].split()[0]: b for b in ctx.split("## ")[1:]}   # "<id> [confidence: …]"
    assert "FX-B05" in blocks and "Scope: installer 1.4x on p5 instances" in blocks["FX-B05"]   # scope lives only in metadata
    assert "FX-B01" in blocks and "Scope:" not in blocks["FX-B01"]                            # control: unscoped rule, no line


def test_pack_ingested_scope_reaches_the_hook(tmp_path):
    import importlib
    sys.path.insert(0, str(HOOK.parent.parent.parent))
    pack = importlib.import_module("loglearner.pack")
    bank = tmp_path / "banks" / "global" / "instincts.jsonl"; bank.parent.mkdir(parents=True)
    rec = {"id": "k2-001", "title": "t", "text": "Use the capacity block offering id when purchasing GPU capacity.",
           "category": "aws-gpu-capacity", "tags": ["aws", "ec2"], "confidence": "high", "source": "team practice",
           "version_scope": "p5/p5en Capacity Blocks", "as_of": "2026-10-13", "source_type": "team-practice", "model_likely_knows": False}
    row = pack.to_instinct_row(rec, "team", "v1", "sha")
    assert row["version_scope"] == "p5/p5en Capacity Blocks" and row["as_of"] == "2026-10-13" and row["model_likely_knows"] is False
    bank.write_text(json.dumps(row) + "\n")
    _d, ctx = run_bash(tmp_path / "s", "aws ec2 purchase-capacity-block --capacity-block-offering-id cbo-1", session="k2",
                       INSTINCTS_JSON=str(bank), FLEET_LEARNINGS_DIR=str(tmp_path / "none"))
    assert "## k2-001" in ctx and "Scope: p5/p5en Capacity Blocks" in ctx


# ---- 0.1.6: delivery in a full bank is decided by relevance, not file position (validator finding) ----
def _bank_with(tmp_path, rows):
    bank = tmp_path / "banks" / "global" / "instincts.jsonl"; bank.parent.mkdir(parents=True, exist_ok=True)
    bank.write_text("".join(json.dumps(r) + "\n" for r in rows)); return bank


def test_late_appended_exact_match_beats_earlier_generic_records(tmp_path):
    generic = [{"id": f"gen-{i:03d}", "rule": f"Generic docker image guidance number {i}: keep layers small and pin the base image.",
                "status": "positive", "confidence": "high", "evidence": "pack", "source": "pack", "tags": ["docker"]} for i in range(12)]
    exact = {"id": "late-001", "rule": "Set EFA_VERSION explicitly in the docker build args; an unpinned EFA_VERSION drifts between builds.",
             "status": "positive", "confidence": "med", "evidence": "pack", "source": "pack", "tags": ["docker"]}
    bank = _bank_with(tmp_path, generic + [exact])                      # appended LAST, lower confidence than the generics
    _d, ctx = run_bash(tmp_path, "docker build --build-arg EFA_VERSION=1.48.0 -t app .", session="rank1",
                       INSTINCTS_JSON=str(bank), FLEET_LEARNINGS_DIR=str(tmp_path / "none"))
    inc = manifest_of(ctx)["included"]
    assert "late-001" in inc, inc                                        # delivered despite the cap and its position
    assert inc[0] == "late-001", inc                                     # and ranked first: exact identifier match wins
    assert "Why these:" in ctx and "TOKEN_INSTINCT_LIMIT=" in ctx        # the "why these" line names the ranking + caps


def test_string_confidence_ranks_and_ties_break_newest_first(tmp_path):
    rows = [{"id": "low-first", "rule": "docker build caches wheels; clear the cache before a reproducibility build.", "confidence": "low",
             "status": "positive", "evidence": "p", "source": "p"},
            {"id": "high-second", "rule": "docker build caches wheels; verify the wheel hash before a reproducibility build.", "confidence": "high",
             "status": "positive", "evidence": "p", "source": "p"},
            {"id": "tie-old", "rule": "docker push needs the registry login refreshed daily.", "confidence": "med", "status": "positive", "evidence": "p", "source": "p"},
            {"id": "tie-new", "rule": "docker push needs the registry login refreshed hourly.", "confidence": "med", "status": "positive", "evidence": "p", "source": "p"}]
    bank = _bank_with(tmp_path, rows)
    _d, ctx = run_bash(tmp_path, "docker push app:dev", session="rank2", INSTINCTS_JSON=str(bank),
                       FLEET_LEARNINGS_DIR=str(tmp_path / "none"), INSTINCT_LIMIT="4", TOKEN_INSTINCT_LIMIT="4")
    inc = manifest_of(ctx)["included"]
    assert inc.index("high-second") < inc.index("low-first"), inc        # high outranks low (both strings)
    assert inc.index("tie-new") < inc.index("tie-old"), inc              # equal score + confidence: newest (later line) first


# ---- agent-engineering class: configuration edits and agent tooling commands route to it (operator direction) ----
def test_agent_config_paths_route_to_the_class_and_infra_paths_do_not():
    m = _hook_module()
    for p in ("/srv/u/.claude/settings.json", "/srv/u/.claude/settings.local.json", "/repo/.claude/hooks/pretooluse.py",
              "/repo/.claude/skills/deploy/SKILL.md", "/repo/AGENTS.md", "/repo/CLAUDE.md", "/srv/u/.codex/config.toml",
              "/repo/.claude/rules/pm-invariants.md", "/srv/u/.claude/projects/-p/memory/feedback_iterate.md",
              "/srv/agent/memory/reference_board.md", "/srv/agent/memory/MEMORY.md"):
        assert m.route(p) is m.AGENT_KWS, p
    assert m.route("/x/memory/notes.md") == []   # a memory dir alone is not enough; the file must be an agent memory file
    assert m.route("/x/infra/firewall-rules.tf") == ["aws", "eks", "ec2", "kubernetes"]   # no theft of infra files
    assert m.route("/work/p/pretool-hook.py") is m.AGENT_KWS                             # singular "hook" token
    assert m.route("/work/p/fix.env") == ["env", "environment", "secret", "settings"]
    assert m.route("/work/p/.env.local") == ["env", "environment", "secret", "settings"]
    assert m.route_command("bash run-fire.sh")[0] == ["script"]
    assert m.route("/x/data/foo.json") == [] and m.route("/x/docs/notes.md") == []
    assert m.route_command("tmux send-keys -t lane 'claude -p hi' Enter")[0] == ["agent-tooling"]
    assert m.route_command("codex exec --profile safe 'fix tests'")[0] == ["agent-tooling"]


def test_settings_edit_delivers_the_class_record_with_scope(tmp_path):
    _d, ctx = run(tmp_path, "/srv/u/.claude/settings.json", content='{"hooks": {"PreToolUse": []}}', session="agent1")
    assert "FX-A01" in ids(ctx) and "Scope: Claude Code 2.1.x" in ctx
    assert manifest_of(ctx)["route_label"] == "agent-config"


def test_unrelated_json_edit_stays_quiet(tmp_path):
    assert run(tmp_path, "/x/data/foo.json", content="{}", session="agent2") == (None, "")


def test_tmux_and_claude_commands_deliver_the_class_record(tmp_path):
    _d, ctx = run_bash(tmp_path, "tmux send-keys -t lane 'claude -p \"summarize\"' Enter", session="agent3")
    assert "FX-A01" in ids(ctx) and "route: bash:agent-tooling" in ctx


# ---- shell tools and git subcommands route (knowledge-lane finding: grep/tail/awk/pkill/echo/cat never routed; every
#      git command drew the same GitHub records) ----
def test_shell_tools_route_with_the_tools_present_as_keywords():
    m = _hook_module()
    labels, kws = m.route_command("pkill -f python3 && tail -n 50 run.log | grep -c ERROR")
    assert labels == ["shell"] and {"quoting", "marker", "regex", "process", "pkill", "tail", "grep"} <= set(kws)
    assert "shell" not in kws and "bash" not in kws                  # broad words only on a .sh Write
    assert m.route_command("docker ps | grep app")[0] == ["containers", "shell"]
    assert m.route_command("ls -la")[0] == []                       # plain listing is not a shell-mechanics event


def test_git_and_gh_route_with_their_subcommand():
    m = _hook_module()
    assert m.route_command("git rebase -i main") == (["git:rebase"], ["git", "rebase"])
    assert m.route_command("git push --force-with-lease origin x") == (["git:push"], ["git", "push"])
    assert m.route_command("gh pr view 29 --json state") == (["gh:pr"], ["github", "gh", "pr", "pull_request"])
    assert m.route_command("git -C /repo status")[0] == ["git:status"]   # the subcommand is found past the option


def test_pkill_command_delivers_the_mechanical_trap_record(tmp_path):
    _d, ctx = run_bash(tmp_path, "pkill -f 'python3 dashboard/app.py'", session="sh1")
    assert ids(ctx)[0] == "FX-S03" and "route: bash:shell" in ctx


def test_grep_command_delivers_the_grep_record_and_echo_alone_is_quiet(tmp_path):
    _d, ctx = run_bash(tmp_path, "grep -rn 'error' logs/ | head", session="sh2")
    assert "FX-S04" in ids(ctx)
    assert run_bash(tmp_path / "e", "echo done", session="sh3") == (None, "")   # no echo-tagged record in the fixture


def test_git_rebase_draws_the_rebase_record_ahead_of_the_generic_github_one(tmp_path):
    _d, ctx = run_bash(tmp_path, "git rebase -i main", session="g1")
    inc = manifest_of(ctx)["included"]
    assert inc.index("FX-G01") < inc.index("FX-G00"), inc
    _d, ctx2 = run_bash(tmp_path / "b", "gh pr view 29", session="g2")
    assert manifest_of(ctx2)["included"][0] == "FX-G00"


def test_shell_script_write_carries_shell_vocabulary(tmp_path):
    _d, ctx = run(tmp_path, "/x/scripts/cleanup.sh", content="set -e\nfor d in $DIRS; do rm -rf $d; done\n", session="sh4")
    assert "FX-S05" in ids(ctx)                                        # no aws/docker/git word in the rule, shell tag only


# ---- repository-state records ride on inspection commands, after the command-specific ones (lift battery finding) ----
def test_inspection_commands_are_recognised():
    m = _hook_module()
    assert m.inspection_command("git status") and m.inspection_command("git -C /r log --oneline -3")
    assert m.inspection_command("find . -name .git -type d") and m.inspection_command("git worktree list")
    assert not m.inspection_command("git add -A") and not m.inspection_command("git commit -m x") and not m.inspection_command("ls -la")


def test_git_status_with_a_full_cap_still_delivers_the_state_record_second(tmp_path):
    # the 3001 case: the cap is filled by status-specific records (INSTINCT_LIMIT=1 -> only FX-G03 fits the primary
    # route); the nested-repo rule must still arrive, after it, through the state tier
    _d, ctx = run_bash(tmp_path, "git status", session="st1", INSTINCT_LIMIT="1")
    man = manifest_of(ctx)
    assert man["included"][:2] == ["FX-G03", "FX-G02"], man["included"]
    assert man["state_tier"] == ["FX-G02"]


def test_git_log_and_find_deliver_the_state_record_but_commit_does_not_use_the_tier(tmp_path):
    _d, ctx = run_bash(tmp_path / "l", "git log --oneline -3", session="st2", INSTINCT_LIMIT="1")
    assert "FX-G02" in manifest_of(ctx)["included"]      # via the primary route or the tier, whichever ranks it in
    _d, ctx = run_bash(tmp_path / "f", "find . -name .git -type d", session="st3")
    assert manifest_of(ctx)["state_tier"] == ["FX-G02"]  # find has no git route at all: only the tier can bring it
    _d, ctx = run_bash(tmp_path / "c", "git commit -m 'x'", session="st9")
    assert manifest_of(ctx)["state_tier"] == []          # a non-inspection command: no second tier


# ---- retired / unservable records are never delivered (knowledge-fabric review F6) ----
def test_retired_twin_is_skipped_while_the_live_record_and_a_negative_warning_are_delivered(tmp_path):
    _d, ctx = run_bash(tmp_path, "docker build -t app:dev .", session="ret1", INSTINCT_LIMIT="20", TOKEN_INSTINCT_LIMIT="20")
    delivered = ids(ctx)
    assert "FX-R01" in delivered                 # live record
    assert "FX-R01-old" not in delivered         # status: deprecated (a tombstoned twin)
    assert "FX-R02-dormant" not in delivered     # lifecycle_state: dormant
    _d, ctx2 = run_bash(tmp_path / "n", "pkill -f worker", session="ret2")
    assert "FX-S03" in ids(ctx2)                 # status: negative is a warning, not a tombstone — still delivered
