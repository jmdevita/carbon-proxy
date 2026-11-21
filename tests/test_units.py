"""Unit tests for logic that needs no running server."""
import asyncio
import json
import struct
import time

import httpx
import pytest

from api import offsets
from config import settings
from core.proxy import _extract_tokens, _merge_stream_usage
from energy import carbon
from energy.power import PowerMonitor, _lookup_cpu_tdp


# --- token extraction -------------------------------------------------------

def test_extract_tokens_openai_subtracts_cached():
    data = {"usage": {"prompt_tokens": 100, "completion_tokens": 7,
                      "prompt_tokens_details": {"cached_tokens": 60}}}
    assert _extract_tokens(data) == (40, 7, 60)


def test_extract_tokens_tolerates_null_usage():
    assert _extract_tokens({"usage": None}) == (0, 0, 0)


def _stream_totals(events):
    totals = {"tokens_in": 0, "tokens_out": 0, "cached_tokens": 0}
    for event in events:
        _merge_stream_usage(totals, "data: " + (event if isinstance(event, str) else json.dumps(event)))
    return totals["tokens_in"], totals["tokens_out"]


def test_stream_usage_openai_final_chunk():
    assert _stream_totals([
        {"choices": [{"delta": {"content": "x"}}], "usage": None},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        "[DONE]",
    ]) == (11, 7)


def test_stream_usage_anthropic_split_across_events():
    # Input tokens arrive first, output tokens later, and the last event has neither.
    assert _stream_totals([
        {"type": "message_start", "message": {"usage": {"input_tokens": 21, "output_tokens": 1}}},
        {"type": "message_delta", "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ]) == (21, 9)


def test_stream_usage_responses_api_nested():
    assert _stream_totals([
        {"type": "response.completed", "response": {"usage": {"input_tokens": 31, "output_tokens": 13}}},
    ]) == (31, 13)


def test_stream_usage_llamacpp_native_and_garbage():
    assert _stream_totals([
        '{"usage": not json',
        {"content": "", "tokens_predicted": 5, "tokens_evaluated": 9},
    ]) == (9, 5)


# --- power ------------------------------------------------------------------

def _fake_card(tmp_path, revision):
    hwmon = tmp_path / "device" / "hwmon" / "hwmon5"
    hwmon.mkdir(parents=True)
    power = hwmon / "power1_average"
    power.write_text("90000000")
    if revision is not None:
        (tmp_path / "device" / "gpu_metrics").write_bytes(struct.pack("<HBB", 264, revision, 0) + b"\0" * 260)
    return str(power)


@pytest.mark.parametrize("revision,integrated", [(1, False), (2, True), (3, True), (None, False)])
def test_integrated_gpu_detected_from_metrics_revision(tmp_path, revision, integrated):
    assert PowerMonitor._hwmon_gpu_is_integrated(_fake_card(tmp_path, revision)) is integrated


def test_cpu_tdp_table_is_loaded():
    tdp, _ = _lookup_cpu_tdp("Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz")
    assert tdp == 95.0


# --- carbon intensity -------------------------------------------------------

def test_electricitymap_failure_backs_off_and_keeps_last_value(monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        raise RuntimeError("api down")

    monkeypatch.setattr(settings, "electricitymap_api_key", "k")
    monkeypatch.setattr(settings, "electricitymap_zone", "Z")
    monkeypatch.setattr(carbon, "_fetch_electricitymap_intensity", fetch)
    monkeypatch.setattr(carbon, "_cached_intensity", 123.0)
    monkeypatch.setattr(carbon, "_cache_time", time.monotonic() - carbon._CACHE_TTL - 1)
    monkeypatch.setattr(carbon, "_retry_not_before", 0)

    assert carbon.get_carbon_intensity() == 123.0  # stale reading, not the static default
    assert carbon.get_carbon_intensity() == 123.0
    assert len(calls) == 1  # second call is inside the backoff window


# --- database ---------------------------------------------------------------

def _insert(db, timestamp, source="a", co2=1.0):
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO requests (timestamp, source, tokens_in, tokens_out, energy_joules, co2_grams, local_date) "
        "VALUES (?, ?, 10, 5, 3600.0, ?, ?)",
        (timestamp, source, co2, db._local_date(timestamp)),
    )
    conn.commit()


def test_daily_breakdown_buckets_by_report_timezone(fresh_db):
    # 03:30 UTC on the 2nd is still the evening of the 1st in New York.
    _insert(fresh_db, "2026-06-02T03:30:00+00:00")
    _insert(fresh_db, "2026-06-02T15:00:00+00:00")
    _insert(fresh_db, "2026-06-02T16:00:00+00:00", source="b")
    rows = {(r["date"], r["source"]): r for r in fresh_db.get_daily_breakdown()}
    assert set(rows) == {("2026-06-01", "a"), ("2026-06-02", "a"), ("2026-06-02", "b")}
    assert rows[("2026-06-02", "a")]["requests"] == 1
    assert rows[("2026-06-02", "a")]["total_tokens"] == 15
    assert rows[("2026-06-02", "a")]["energy_kwh"] == pytest.approx(0.001)


def test_local_date_backfilled_for_existing_rows(fresh_db):
    conn = fresh_db._get_conn()
    conn.execute("INSERT INTO requests (timestamp, source) VALUES ('2026-06-02T03:30:00+00:00', 'old')")
    conn.commit()
    fresh_db.init_db()
    assert conn.execute("SELECT local_date FROM requests").fetchone()[0] == "2026-06-01"


def test_log_request_stamps_active_key_only(fresh_db):
    fresh_db.api_key_create("h1", "live-key")
    revoked = fresh_db.api_key_create("h2", "dead-key")
    fresh_db.api_key_revoke(revoked)
    fresh_db.log_request("live-key", "m", 1, 1, 10)
    fresh_db.log_request("dead-key", "m", 1, 1, 10)
    used = {k["label"]: k["last_used_at"] for k in fresh_db.api_key_list(include_revoked=True)}
    assert used["live-key"] is not None
    assert used["dead-key"] is None


def test_rename_carries_history_unless_label_is_shared(fresh_db):
    solo = fresh_db.api_key_create("h1", "solo")
    twin = fresh_db.api_key_create("h2", "twin")
    fresh_db.api_key_create("h3", "twin")
    fresh_db.log_request("solo", "m", 1, 1, 10)
    fresh_db.log_request("twin", "m", 1, 1, 10)
    fresh_db.api_key_update_label(solo, "renamed", None)
    fresh_db.api_key_update_label(twin, "other", None)
    assert fresh_db.get_sources() == ["renamed", "twin"]


# --- CNaught orders ---------------------------------------------------------

class FakeCNaught:
    """Stands in for the CNaught API; records every order it accepts."""

    def __init__(self):
        self.orders = []
        self.post_behaviour = "ok"
        self.posts = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": self.orders})
        body = json.loads(request.content)
        key = request.headers["Idempotency-Key"]
        self.posts.append((key, body))
        if self.post_behaviour == "bad_request":
            return httpx.Response(400, json={"title": "bad"})
        existing = next((o for o in self.orders if o["metadata"] == body["metadata"]), None)
        if existing is None:
            existing = {"id": f"order{len(self.orders) + 1}", "amount_kg": body["amount_kg"],
                        "metadata": body["metadata"], "price_usd_cents": 30 * body["amount_kg"],
                        "certificate_public_url": "https://example.test/cert"}
            self.orders.append(existing)
        if self.post_behaviour == "timeout_after_placing":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(201, json=existing)


@pytest.fixture
def cnaught(fresh_db, monkeypatch):
    fake = FakeCNaught()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(settings, "cnaught_api_key", "test")
    monkeypatch.setattr(
        offsets.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(fake.handler), **kw),
    )
    return fake


def test_cnaught_purchase_records_order(cnaught):
    result = asyncio.run(offsets.purchase_cnaught(1500))
    assert (result.co2_grams_offset, result.cost_cents, result.order_id) == (2000, 60, "order1")
    assert asyncio.run(offsets._load_pending_order()) is None


def test_cnaught_timeout_after_placing_is_not_bought_twice(cnaught):
    cnaught.post_behaviour = "timeout_after_placing"
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(offsets.purchase_cnaught(3000))
    assert len(cnaught.orders) == 1

    # The next attempt, even for a different amount, finds the placed order.
    cnaught.post_behaviour = "ok"
    result = asyncio.run(offsets.purchase_cnaught(5000))
    assert result.order_id == "order1"
    assert result.co2_grams_offset == 3000
    assert len(cnaught.orders) == 1
    assert len(cnaught.posts) == 1
    assert asyncio.run(offsets._load_pending_order()) is None


def test_cnaught_definite_rejection_clears_the_attempt(cnaught):
    cnaught.post_behaviour = "bad_request"
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(offsets.purchase_cnaught(1000))
    assert asyncio.run(offsets._load_pending_order()) is None

    cnaught.post_behaviour = "ok"
    asyncio.run(offsets.purchase_cnaught(1000))
    assert cnaught.posts[0][0] != cnaught.posts[1][0]  # a rejected key is never reused
