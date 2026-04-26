import sqlite3
import asyncio
import threading
import logging
from datetime import datetime, timezone

from config import settings

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

        CREATE INDEX IF NOT EXISTS idx_requests_source ON requests(source);
        CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
        CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
    """)
    # Idempotent migration: add is_auto column to offsets if missing
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(offsets)").fetchall()]
    if "is_auto" not in cols:
        conn.execute("ALTER TABLE offsets ADD COLUMN is_auto INTEGER NOT NULL DEFAULT 0")
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
):
    with _db_lock:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO requests
               (timestamp, source, model, tokens_in, tokens_out, duration_ms,
                energy_joules, co2_grams, power_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, source, model, tokens_in, tokens_out, duration_ms,
             energy_joules, co2_grams, power_source),
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
):
    await asyncio.to_thread(
        log_request, source, model, tokens_in, tokens_out,
        duration_ms, energy_joules, co2_grams, power_source,
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
            SELECT
                DATE(timestamp) as date,
                source,
                COUNT(*) as requests,
                SUM(tokens_in + tokens_out) as total_tokens,
                SUM(energy_joules) / 3600000.0 as energy_kwh,
                SUM(co2_grams) / 1000.0 as co2_kg
            FROM requests {where}
            GROUP BY DATE(timestamp), source
            ORDER BY date DESC
        """
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


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
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_cents), 0) AS total FROM offsets "
            "WHERE is_auto = 1 AND DATE(timestamp) = DATE('now')"
        ).fetchone()
        return int(row["total"])


def close_db():
    global _conn
    with _db_lock:
        if _conn:
            _conn.close()
            _conn = None
