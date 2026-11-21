"""End-to-end tests: a real carbon-proxy process in front of a fake upstream."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"Authorization": "Bearer test-admin-key"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_up(url: str):
    for _ in range(100):
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


class Stack:
    def __init__(self, proxy: str, upstream: str, db_path: str, upstream_proc):
        self.proxy, self.upstream, self.db_path, self.upstream_proc = proxy, upstream, db_path, upstream_proc

    def query(self, sql: str):
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def last_request(self):
        return self.query("SELECT model, tokens_in, tokens_out FROM requests ORDER BY id DESC LIMIT 1")[0]

    def request_count(self) -> int:
        return self.query("SELECT COUNT(*) FROM requests")[0][0]

    def active(self) -> int:
        return httpx.get(self.proxy + "/carbon/live", timeout=5).json()["active_requests"]

    def settle(self, expected_rows: int):
        """Wait for the background logging task to write its row."""
        for _ in range(50):
            if self.request_count() >= expected_rows:
                return
            time.sleep(0.1)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("stack")
    up_port, proxy_port = _free_port(), _free_port()
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "UPSTREAM_URL": f"http://127.0.0.1:{up_port}",
        "SQLITE_PATH": str(tmp / "carbon.db"),
    }
    uvicorn = [sys.executable, "-m", "uvicorn", "--log-level", "warning", "--host", "127.0.0.1"]
    upstream = subprocess.Popen([*uvicorn, "fake_upstream:app", "--port", str(up_port)],
                                cwd=ROOT / "tests", env=env)
    proxy = subprocess.Popen([*uvicorn, "main:app", "--port", str(proxy_port)], cwd=ROOT, env=env)
    try:
        _wait_up(f"http://127.0.0.1:{up_port}/_state")
        _wait_up(f"http://127.0.0.1:{proxy_port}/health")
        yield Stack(f"http://127.0.0.1:{proxy_port}", f"http://127.0.0.1:{up_port}",
                    str(tmp / "carbon.db"), upstream)
    finally:
        for proc in (proxy, upstream):
            proc.terminate()
            proc.wait(timeout=10)


def _post(stack, path, body, **kw):
    before = stack.request_count()
    resp = httpx.post(stack.proxy + path, json=body, timeout=20, **kw)
    stack.settle(before + 1)
    return resp


@pytest.mark.parametrize("path,model,expected", [
    ("/v1/chat/completions", "openai", (11, 7)),
    ("/v1/messages", "anthropic", (21, 9)),
    ("/v1/responses", "responses", (31, 13)),
])
def test_streaming_tokens_are_logged(stack, path, model, expected):
    _post(stack, path, {"stream": True, "model": model})
    assert stack.last_request() == (model, *expected)


def test_non_streaming_response_and_tokens(stack):
    resp = _post(stack, "/v1/chat/completions", {"model": "plain"})
    assert resp.json()["usage"]["completion_tokens"] == 3
    assert stack.last_request() == ("plain", 5, 3)


def test_gzipped_upstream_body_is_readable(stack):
    resp = _post(stack, "/v1/chat/completions", {"kind": "gzip", "model": "gz"})
    assert resp.status_code == 200
    assert resp.json()["usage"]["prompt_tokens"] == 5


def test_client_disconnect_cancels_upstream_and_logs_once(stack):
    before = stack.request_count()
    with httpx.stream("POST", stack.proxy + "/v1/chat/completions",
                      json={"stream": True, "kind": "slow", "model": "slow"}, timeout=20) as resp:
        for i, _ in enumerate(resp.iter_raw()):
            if i >= 2:
                break
    stack.settle(before + 1)
    time.sleep(0.5)
    assert stack.active() == 0
    assert stack.request_count() == before + 1
    assert httpx.get(stack.upstream + "/_state").json()["slow_cancelled"] is True


def test_upstream_dying_mid_stream_is_logged_not_leaked(stack):
    before = stack.request_count()
    try:
        httpx.post(stack.proxy + "/v1/chat/completions",
                   json={"stream": True, "kind": "die", "model": "die"}, timeout=20)
    except httpx.HTTPError:
        pass
    stack.settle(before + 1)
    assert stack.active() == 0
    assert stack.request_count() == before + 1


def test_non_object_json_body_is_forwarded(stack):
    resp = httpx.post(stack.proxy + "/v1/echo-status", content=b"[1,2,3]",
                      headers={"content-type": "application/json"}, timeout=20)
    assert resp.status_code == 404  # the upstream's answer, not a proxy crash
    assert stack.active() == 0


def test_blank_label_is_rejected(stack):
    con = sqlite3.connect(stack.db_path)
    con.execute("INSERT INTO api_keys (key_hash, label, notes, created_at) "
                "VALUES ('abc', 'orig', '', '2026-01-01T00:00:00+00:00')")
    con.commit()
    key_id = con.execute("SELECT id FROM api_keys WHERE key_hash = 'abc'").fetchone()[0]
    con.close()

    resp = httpx.patch(f"{stack.proxy}/carbon/api_keys/{key_id}", json={"label": "   "}, headers=ADMIN)
    assert resp.status_code == 400
    resp = httpx.patch(f"{stack.proxy}/carbon/api_keys/{key_id}", json={"notes": "n"}, headers=ADMIN)
    assert (resp.status_code, resp.json()["label"], resp.json()["notes"]) == (200, "orig", "n")


def test_admin_endpoints_reject_wrong_key(stack):
    resp = httpx.patch(f"{stack.proxy}/carbon/api_keys/1", json={"notes": "n"},
                       headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_worker_validated_request_stamps_last_used(stack):
    _post(stack, "/v1/chat/completions", {"model": "m"}, headers={"x-carbon-source": "orig"})
    assert stack.query("SELECT last_used_at IS NOT NULL FROM api_keys WHERE label = 'orig'") == [(1,)]


def test_daily_endpoint_groups_logged_requests(stack):
    rows = httpx.get(stack.proxy + "/carbon/daily").json()
    assert sum(r["requests"] for r in rows) == stack.request_count()
    assert all(len(r["date"]) == 10 for r in rows)
    summary = httpx.get(stack.proxy + "/carbon/summary").json()
    assert "equivalents" in summary


def test_unreachable_upstream_is_a_502(stack):
    # Runs last: it stops the fake upstream for the rest of the module.
    stack.upstream_proc.terminate()
    stack.upstream_proc.wait(timeout=10)
    for body in ({"model": "m"}, {"model": "m", "stream": True}):
        resp = httpx.post(stack.proxy + "/v1/chat/completions", json=body, timeout=20)
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "upstream_error"
    assert httpx.get(stack.proxy + "/v1/models", timeout=20).status_code == 502
    assert stack.active() == 0
