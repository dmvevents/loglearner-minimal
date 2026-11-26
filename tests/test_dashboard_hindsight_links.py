"""Links to the Hindsight memory API on the dashboard pages are built from a browser-facing base, not from the address
this server uses to reach the API. Found by the harness on the vendored 0.1.10: pages served through its proxy linked
14 "entity graph JSON" hrefs at the container-internal address, unreachable from a browser and, on a shared host,
somebody else's Hindsight. Hermetic: a stub API on an ephemeral loopback port, HOME and every LOGLEARNER_* knob under
tmp_path, the module imported in a subprocess."""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BANKS = [{"bank_id": "alpha", "name": "first bank"}, {"bank_id": "beta", "name": "second bank"}]


class _Stub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server API
        body = json.dumps(BANKS if self.path == "/v1/default/banks" else {"error": "no such path"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def stub_api():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _render(tmp_path, api, link_base=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOGLEARNER_") and k != "HINDSIGHT_API"}
    env.update({"HOME": str(tmp_path / "home"), "LOGLEARNER_DB": str(tmp_path / "index.db"),
                "LOGLEARNER_BANKS_DIR": str(tmp_path / "banks"), "LOGLEARNER_UCB_DIR": str(tmp_path / "ucb"),
                "LOGLEARNER_KNOWLEDGE_DIR": str(tmp_path / "no-store"), "LOGLEARNER_INSTINCT_LIBRARIES_DIR": str(tmp_path / "no-libraries"),
                "LOGLEARNER_GIST_INDEX": str(tmp_path / "no-gist-index.jsonl"), "PYTHONPATH": str(REPO), "HINDSIGHT_API": api})
    if link_base is not None:
        env["LOGLEARNER_HINDSIGHT_LINK_BASE"] = link_base
    code = "from dashboard import app; print(app.h_hindsight({}))"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return r.stdout


def _graph_hrefs(page):
    return re.findall(r'href="([^"]*/v1/default/banks/[^"]+/graph)"', page)


def test_links_use_the_browser_facing_base_when_configured(tmp_path, stub_api):
    page = _render(tmp_path, stub_api, link_base="/memory-api")
    assert _graph_hrefs(page) == ["/memory-api/v1/default/banks/alpha/graph", "/memory-api/v1/default/banks/beta/graph"]
    assert stub_api not in page            # the server-side address appears nowhere on the page
    assert "127.0.0.1:8888" not in page


def test_links_default_to_the_server_address_and_the_server_address_is_configurable(tmp_path, stub_api):
    # control: the stub (not the hard-coded 127.0.0.1:8888 of 0.1.10) answered the banks list, and without a link base
    # the hrefs point where the server reached it
    page = _render(tmp_path, stub_api)
    assert _graph_hrefs(page) == [f"{stub_api}/v1/default/banks/alpha/graph", f"{stub_api}/v1/default/banks/beta/graph"]
    assert "127.0.0.1:8888" not in page


def test_trailing_slash_on_the_base_does_not_double_up(tmp_path, stub_api):
    page = _render(tmp_path, stub_api, link_base="/memory-api/")
    assert _graph_hrefs(page)[0] == "/memory-api/v1/default/banks/alpha/graph"
