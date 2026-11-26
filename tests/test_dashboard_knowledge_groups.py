"""/knowledge groups records by class with counts and a class filter (owner question: the EFA/EKS topics were invisible).
Hermetic: HOME, banks, db and ledgers under tmp_path. Red on the parent (no 'Knowledge by class' section)."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def build(tmp_path):
    bank = tmp_path / "banks" / "global" / "instincts.jsonl"
    bank.parent.mkdir(parents=True)
    rows = [
        {"id": "eks-1", "rule": "Taint GPU node groups so only GPU workloads schedule there.", "status": "positive", "confidence": "high",
         "evidence": "p", "source": "p", "category": "eks-k8s-gpu", "tags": ["kubectl"]},
        {"id": "eks-2", "rule": "Pin the EFA device plugin version to the node AMI release.", "status": "positive", "confidence": "high",
         "evidence": "p", "source": "p", "category": "eks-k8s-gpu", "tags": ["kubectl"]},
        {"id": "eks-3", "rule": "Never delete a node group that still holds a capacity reservation.", "status": "negative", "confidence": "high",
         "evidence": "p", "source": "p", "category": "eks-k8s-gpu", "version_scope": "EKS 1.3x"},
        {"id": "efa-1", "rule": "Add the self-referencing security-group egress rule before the first fabric test.", "status": "positive",
         "confidence": "high", "evidence": "p", "source": "p", "category": "efa-networking-practical", "tags": ["fi_info"]},
        {"id": "efa-2", "rule": "Use the primary VPC CIDR for fabric traffic; secondary CIDRs are dropped silently.", "status": "negative",
         "confidence": "high", "evidence": "p", "source": "p", "category": "efa-networking-practical"},
        {"id": "misc-1", "rule": "A record without a class still shows up under uncategorized.", "status": "positive", "confidence": "med",
         "evidence": "p", "source": "p"},
    ]
    bank.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "home" / ".claude" / "projects").mkdir(parents=True)
    (tmp_path / "ucb").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOGLEARNER_")}
    env.update({"HOME": str(tmp_path / "home"), "LOGLEARNER_DB": str(tmp_path / "index.db"), "LOGLEARNER_BANKS_DIR": str(tmp_path / "banks"),
                "LOGLEARNER_UCB_DIR": str(tmp_path / "ucb"), "LOGLEARNER_KNOWLEDGE_DIR": str(tmp_path / "no-store"),
                "LOGLEARNER_HINDSIGHT_DISABLE": "1", "LOGLEARNER_INSTINCT_LIBRARIES_DIR": str(tmp_path / "no-libraries"),
                "LOGLEARNER_GIST_INDEX": str(tmp_path / "no-gist-index.jsonl"), "PYTHONPATH": str(REPO)})
    r = subprocess.run([sys.executable, str(REPO / "dashboard" / "indexer.py"), "--db", str(tmp_path / "index.db")],
                       capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300)
    assert r.returncode == 0, r.stderr[-800:]
    return env


def render(env, code):
    r = subprocess.run([sys.executable, "-c", "import json,sys; sys.path.insert(0, %r); from dashboard import app\n%s" % (str(REPO), code)],
                       capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120)
    assert r.returncode == 0, r.stderr[-1200:]
    return r.stdout


def test_knowledge_page_groups_by_class_with_counts_and_filter_options(tmp_path):
    env = build(tmp_path)
    page = render(env, "print(app.h_knowledge({}))")
    assert "Knowledge by class" in page and "3 classes across 6 records" in page
    row = re.search(r"<tr><td data-label='class'><a href='/knowledge\?cls=eks-k8s-gpu'><b>eks-k8s-gpu</b></a></td>"
                    r"<td data-label='records' class='num'>3</td><td data-label='positive' class='num'>2</td>"
                    r"<td data-label='negative' class='num'>1</td>", page)
    assert row, "eks group row with counts"
    assert "<b>efa-networking-practical</b>" in page and "<b>(uncategorized)</b>" in page
    assert '<option value="eks-k8s-gpu" >eks-k8s-gpu (3)</option>' in page
    assert '<option value="efa-networking-practical" >efa-networking-practical (2)</option>' in page
    assert "all classes (6 records)" in page
    assert page.count("stacked-table") >= 3                 # group table + the banks section tables render as cards on phones
    assert not re.search(r"(?<![/_.\-`A-Za-z0-9])(?:reference|reference|reference)(?![/_.\-`A-Za-z0-9])", page)


def test_class_filter_shows_only_that_class_with_an_honest_count(tmp_path):
    env = build(tmp_path)
    page = render(env, "print(app.h_knowledge({'cls': ['efa-networking-practical']}))")
    assert "Records in class efa-networking-practical" in page and "showing 2 of 2 in efa-networking-practical" in page
    assert "efa-1" in page and "efa-2" in page and "eks-1" not in page.split("Records in class")[1]
    assert "selected" in page and 'href="/knowledge">clear</a>' in page
    page2 = render(env, "print(app.h_knowledge({'cls': ['eks-k8s-gpu']}))")
    assert "showing 3 of 3 in eks-k8s-gpu" in page2 and "EKS 1.3x" in page2   # scope column
