import sqlite3
import asyncio
import threading
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import settings


def _report_tz() -> ZoneInfo:
    return ZoneInfo(settings.report_timezone)


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local_date(ts: str) -> str:
    return _parse_utc(ts).astimezone(_report_tz()).date().isoformat()

logger = logging.getLogger("carbon-proxy.db")

_conn: sqlite3.Connection | None = None
_db_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.sqlite_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            energy_joules REAL NOT NULL DEFAULT 0.0,
            co2_grams REAL NOT NULL DEFAULT 0.0,
            power_source TEXT NOT NULL DEFAULT 'none'
        );

        CREATE TABLE IF NOT EXISTS daily_summary (
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            total_energy_kwh REAL NOT NULL DEFAULT 0.0,
            total_co2_kg REAL NOT NULL DEFAULT 0.0,
            trees_to_offset REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (date, source)
        );

        CREATE TABLE IF NOT EXISTS offsets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            provider TEXT NOT NULL,
            co2_grams_offset REAL NOT NULL DEFAULT 0.0,
            cost_cents INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
            certificate_url TEXT NOT NULL DEFAULT '',
            order_id TEXT NOT NULL DEFAULT '',
            tree_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS kv_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_requests_source ON requests(source);
        CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
        CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
        CREATE INDEX IF NOT EXISTS idx_api_keys_revoked ON api_keys(revoked_at);
    """)
    # Idempotent migration: add is_auto column to offsets if missing
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(offsets)").fetchall()]
    if "is_auto" not in cols:
        conn.execute("ALTER TABLE offsets ADD COLUMN is_auto INTEGER NOT NULL DEFAULT 0")
    # Idempotent migration: add cached_tokens column to requests if missing.
    # Tracks KV-cache prefix hits; excluded from tokens_in (which is fresh-only).
    req_cols = [r["name"] for r in conn.execute("PRAGMA table_info(requests)").fetchall()]
    if "cached_tokens" not in req_cols:
        conn.execute("ALTER TABLE requests ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    logger.info("Database initialized at %s", settings.sqlite_path)


def log_request(
    source: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    duration_ms: int,
    energy_joules: float = 0.0,
    co2_grams: float = 0.0,
    power_source: str = "none",
    cached_tokens: int = 0,
):
    with _db_lock:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO requests
               (timestamp, source, model, tokens_in, tokens_out, duration_ms,
                energy_joules, co2_grams, power_source, cached_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, source, model, tokens_in, tokens_out, duration_ms,
             energy_joules, co2_grams, power_source, cached_tokens),
        )
        conn.commit()


async def log_request_async(
    source: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    duration_ms: int,
    energy_joules: float = 0.0,
    co2_grams: float = 0.0,
    power_source: str = "none",
    cached_tokens: int = 0,
):
    await asyncio.to_thread(
        log_request, source, model, tokens_in, tokens_out,
        duration_ms, energy_joules, co2_grams, power_source, cached_tokens,
    )


def get_requests(
    source: str | None = None,
    model: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    with _db_lock:
        conn = _get_conn()
        conditions = []
        params = []

        if source:
            conditions.append("source = ?")
            params.append(source)
        if model:
            conditions.append("model = ?")
            params.append(model)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM requests {where} ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_summary(
    source: str | None = None,
    model: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    with _db_lock:
        conn = _get_conn()
        conditions = []
        params = []

        if source:
            conditions.append("source = ?")
            params.append(source)
        if model:
            conditions.append("model = ?")
            params.append(model)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT
                COUNT(*) as total_requests,
                COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                COALESCE(SUM(tokens_out), 0) as total_tokens_out,
                COALESCE(SUM(tokens_in + tokens_out), 0) as total_tokens,
                COALESCE(SUM(energy_joules), 0) as total_energy_joules,
                COALESCE(SUM(energy_joules) / 3600000.0, 0) as total_energy_kwh,
                COALESCE(SUM(co2_grams), 0) as total_co2_grams,
                COALESCE(SUM(co2_grams) / 1000.0, 0) as total_co2_kg
            FROM requests {where}
        """
        row = conn.execute(query, params).fetchone()
        return dict(row)


def get_daily_breakdown(
    source: str | None = None,
    model: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    with _db_lock:
        conn = _get_conn()
        conditions = []
        params = []

        if source:
            conditions.append("source = ?")
            params.append(source)
        if model:
            conditions.append("model = ?")
            params.append(model)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT timestamp, source,
                   COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0) AS tokens,
                   COALESCE(energy_joules, 0) AS energy_joules,
                   COALESCE(co2_grams, 0) AS co2_grams
            FROM requests {where}
        """
        rows = conn.execute(query, params).fetchall()

    buckets: dict[tuple[str, str], dict] = {}
    for r in rows:
        date_str = _local_date(r["timestamp"])
        src = r["source"]
        b = buckets.setdefault((date_str, src), {
            "date": date_str,
            "source": src,
            "requests": 0,
            "total_tokens": 0,
            "energy_kwh": 0.0,
            "co2_kg": 0.0,
        })
        b["requests"] += 1
        b["total_tokens"] += r["tokens"]
        b["energy_kwh"] += r["energy_joules"] / 3600000.0
        b["co2_kg"] += r["co2_grams"] / 1000.0

    return sorted(buckets.values(), key=lambda x: x["date"], reverse=True)


def log_offset(
    provider: str,
    co2_grams_offset: float,
    cost_cents: int,
    currency: str = "USD",
    certificate_url: str = "",
    order_id: str = "",
    tree_count: int = 0,
    is_auto: bool = False,
):
    with _db_lock:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO offsets
               (timestamp, provider, co2_grams_offset, cost_cents, currency,
                certificate_url, order_id, tree_count, is_auto)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, provider, co2_grams_offset, cost_cents, currency,
             certificate_url, order_id, tree_count, 1 if is_auto else 0),
        )
        conn.commit()


async def log_offset_async(**kwargs):
    await asyncio.to_thread(log_offset, **kwargs)


def get_balance() -> dict:
    with _db_lock:
        conn = _get_conn()
        emissions = conn.execute(
            "SELECT COALESCE(SUM(co2_grams), 0) as total FROM requests"
        ).fetchone()
        offsets = conn.execute(
            "SELECT COALESCE(SUM(co2_grams_offset), 0) as total, "
            "COALESCE(SUM(tree_count), 0) as trees, "
            "COALESCE(SUM(cost_cents), 0) as cost "
            "FROM offsets"
        ).fetchone()

    total_emitted = emissions["total"]
    total_offset = offsets["total"]
    balance = total_emitted - total_offset

    return {
        "total_co2_grams": round(total_emitted, 4),
        "total_offset_grams": round(total_offset, 4),
        "balance_grams": round(balance, 4),
        "balance_kg": round(balance / 1000, 6),
        "trees_planted": offsets["trees"],
        "total_cost_cents": offsets["cost"],
    }


def get_offsets(limit: int = 100, offset: int = 0) -> list[dict]:
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM offsets ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def get_sources() -> list[str]:
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT DISTINCT source FROM requests ORDER BY source"
        ).fetchall()
        return [row["source"] for row in rows]


def get_kv(key: str, default: str | None = None) -> str | None:
    with _db_lock:
        conn = _get_conn()
        row = conn.execute("SELECT value FROM kv_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_kv(key: str, value: str):
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO kv_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_today_auto_spend_cents() -> int:
    tz = _report_tz()
    start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).isoformat()
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_cents), 0) AS total FROM offsets "
            "WHERE is_auto = 1 AND timestamp >= ? AND timestamp < ?",
            (start_utc, end_utc),
        ).fetchone()
        return int(row["total"])


def api_key_create(key_hash: str, label: str, notes: str = "") -> int:
    with _db_lock:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO api_keys (key_hash, label, notes, created_at) "
            "VALUES (?, ?, ?, ?)",
            (key_hash, label, notes, now),
        )
        conn.commit()
        return cur.lastrowid


def api_key_delete_local(key_hash: str):
    """Hard delete a local row by hash. Used for rollback when KV write fails."""
    with _db_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM api_keys WHERE key_hash = ?", (key_hash,))
        conn.commit()


def api_key_list(include_revoked: bool = False) -> list[dict]:
    with _db_lock:
        conn = _get_conn()
        where = "" if include_revoked else "WHERE revoked_at IS NULL"
        rows = conn.execute(
            f"SELECT id, key_hash, label, notes, created_at, last_used_at, revoked_at "
            f"FROM api_keys {where} ORDER BY id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def api_key_get(key_id: int) -> dict | None:
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
        return dict(row) if row else None


def api_key_revoke(key_id: int) -> dict | None:
    """Set revoked_at on the row. Returns the row, or None if not found."""
    with _db_lock:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, key_id),
        )
        if cur.rowcount == 0:
            # Either missing or already revoked -- return current state regardless
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            conn.commit()
            return dict(row) if row else None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
        return dict(row) if row else None


def api_key_update_label(key_id: int, label: str | None, notes: str | None) -> dict | None:
    fields = []
    params: list = []
    if label is not None:
        fields.append("label = ?")
        params.append(label)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if not fields:
        return api_key_get(key_id)
    params.append(key_id)
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            f"UPDATE api_keys SET {', '.join(fields)} WHERE id = ?", params
        )
        conn.commit()
    return api_key_get(key_id)


def api_key_lookup_active(key_hash: str) -> dict | None:
    """Return the row for an active (non-revoked) hash, or None."""
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, label FROM api_keys "
            "WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        ).fetchone()
        return dict(row) if row else None


def api_key_touch(key_hash: str):
    """Update last_used_at for an active key. Silently no-ops if missing/revoked."""
    with _db_lock:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? "
            "WHERE key_hash = ? AND revoked_at IS NULL",
            (now, key_hash),
        )
        conn.commit()


def api_key_active_map() -> dict[str, str]:
    """All active keys as {hash: label} -- used by the reconciler."""
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT key_hash, label FROM api_keys WHERE revoked_at IS NULL"
        ).fetchall()
        return {row["key_hash"]: row["label"] for row in rows}


def api_key_stats(label: str, since_iso: str, recent_limit: int = 10) -> dict:
    """Per-key rollup for the dashboard card + drill-down.

    `requests` are keyed by `source` (the label). If two keys share a label
    their stats merge -- acceptable for now since labels are how users
    distinguish keys anyway.

    Returns:
        totals: dict with request count, tokens, energy, co2 since `since_iso`
        daily_series: list of {date, requests, co2_grams} for the past 7 days
            (always 7 entries, oldest first, zero-filled for empty days)
        primary_model: model with the most requests in the window, or None
        recent_requests: latest N requests for this source (any time, capped)
    """
    with _db_lock:
        conn = _get_conn()

        totals_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_requests,
                COALESCE(SUM(tokens_in + tokens_out), 0) AS total_tokens,
                COALESCE(SUM(energy_joules) / 3600000.0, 0) AS total_energy_kwh,
                COALESCE(SUM(co2_grams), 0) AS total_co2_grams
            FROM requests
            WHERE source = ? AND timestamp >= ?
            """,
            (label, since_iso),
        ).fetchone()
        totals = dict(totals_row)

        # Pull raw rows; bucket by report-tz date in Python so day boundaries
        # match the dashboard's displayed timezone rather than UTC.
        daily_raw = conn.execute(
            """
            SELECT timestamp, COALESCE(co2_grams, 0) AS co2_grams
            FROM requests
            WHERE source = ? AND timestamp >= ?
            """,
            (label, since_iso),
        ).fetchall()

        tz = _report_tz()
        daily_by_date: dict[str, dict] = {}
        for r in daily_raw:
            d = _parse_utc(r["timestamp"]).astimezone(tz).date().isoformat()
            b = daily_by_date.setdefault(d, {"date": d, "requests": 0, "co2_grams": 0.0})
            b["requests"] += 1
            b["co2_grams"] += r["co2_grams"]

        # Pad to exactly 7 days ending today in the report timezone so the
        # sparkline has a stable x-axis.
        today = datetime.now(tz).date()
        daily_series = []
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            daily_series.append(
                daily_by_date.get(d, {"date": d, "requests": 0, "co2_grams": 0.0})
            )

        primary_row = conn.execute(
            """
            SELECT model, COUNT(*) AS n
            FROM requests
            WHERE source = ? AND timestamp >= ? AND model != ''
            GROUP BY model
            ORDER BY n DESC
            LIMIT 1
            """,
            (label, since_iso),
        ).fetchone()
        primary_model = primary_row["model"] if primary_row else None

        recent_rows = conn.execute(
            """
            SELECT timestamp, model, tokens_in, tokens_out, duration_ms,
                   energy_joules, co2_grams
            FROM requests
            WHERE source = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (label, recent_limit),
        ).fetchall()

    return {
        "totals": totals,
        "daily_series": daily_series,
        "primary_model": primary_model,
        "recent_requests": [dict(r) for r in recent_rows],
    }


def api_key_revoked_hashes() -> set[str]:
    """Hashes of revoked keys -- reconciler ensures these are absent from KV."""
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT key_hash FROM api_keys WHERE revoked_at IS NOT NULL"
        ).fetchall()
        return {row["key_hash"] for row in rows}


def close_db():
    global _conn
    with _db_lock:
        if _conn:
            _conn.close()
            _conn = None
