"""SQLite 持久化：监控列表、事件流、下单记录、每日预算。

用同步 sqlite3 就够了——单次操作都在毫秒级，
但为了不阻塞事件循环里的冲刺任务，写操作统一走一把线程锁 + WAL。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import DomainState, Event, Phase, RegistrationResult, WatchedDomain
from .utils import iso, parse_datetime, to_utc, utcnow

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    domain               TEXT PRIMARY KEY,
    state                TEXT NOT NULL DEFAULT 'unknown',
    statuses             TEXT NOT NULL DEFAULT '[]',
    registrar            TEXT,
    expires_at           TEXT,
    drop_at              TEXT,
    pending_delete_since TEXT,
    redemption_since     TEXT,
    last_checked_at      TEXT,
    next_check_at        TEXT,
    phase                TEXT NOT NULL DEFAULT 'idle',
    attempts             INTEGER NOT NULL DEFAULT 0,
    max_price            REAL,
    years                INTEGER,
    note                 TEXT,
    source               TEXT NOT NULL DEFAULT 'config',
    group_name           TEXT,
    stop_after_first     INTEGER NOT NULL DEFAULT 0,
    enabled              INTEGER NOT NULL DEFAULT 1,
    added_at             TEXT NOT NULL,
    acquired_at          TEXT,
    last_error           TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    domain     TEXT,
    message    TEXT NOT NULL DEFAULT '',
    data       TEXT
);

CREATE TABLE IF NOT EXISTS purchases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    domain       TEXT NOT NULL,
    provider     TEXT NOT NULL,
    success      INTEGER NOT NULL,
    order_id     TEXT,
    price        REAL,
    currency     TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    message      TEXT,
    raw          TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

"""

# 索引单独一段：老库要先补完列才能建这些索引。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_created  ON events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_domain   ON events(domain, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_purchases_time  ON purchases(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_domains_next    ON domains(enabled, next_check_at);
CREATE INDEX IF NOT EXISTS idx_domains_group   ON domains(group_name);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Storage:
    """所有持久化状态的唯一入口。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)   # 建表（已存在则空操作）
            self._migrate()                     # 给老库补新增的列
            self._conn.executescript(INDEXES)   # 索引可能引用新列，必须最后建
            self._conn.commit()

    def _migrate(self) -> None:
        """给已经存在的老库补上后来新增的列。

        CREATE TABLE IF NOT EXISTS 对已存在的表是空操作，所以新增列必须
        单独 ALTER，否则升级后的用户会撞上 no such column。
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(domains)").fetchall()
        }
        additions = {
            "group_name": "TEXT",
            "stop_after_first": "INTEGER NOT NULL DEFAULT 0",
            "redemption_since": "TEXT",
            "last_error": "TEXT",
        }
        for column, definition in additions.items():
            if column not in existing:
                logger.info("升级数据库：给 domains 表添加 %s 列", column)
                self._conn.execute(f"ALTER TABLE domains ADD COLUMN {column} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ 监控列表

    def upsert_domain(
        self,
        domain: str,
        *,
        max_price: float | None = None,
        years: int | None = None,
        note: str | None = None,
        source: str = "config",
        group: str | None = None,
        stop_after_first: bool = False,
    ) -> bool:
        """加入监控列表，返回 True 表示是新增（而不是更新）。"""
        now = iso(utcnow())
        with self._lock:
            existing = self._conn.execute(
                "SELECT domain FROM domains WHERE domain = ?", (domain,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    """UPDATE domains
                       SET max_price        = COALESCE(?, max_price),
                           years            = COALESCE(?, years),
                           note             = COALESCE(?, note),
                           group_name       = COALESCE(?, group_name),
                           stop_after_first = ?,
                           enabled          = 1
                     WHERE domain = ?""",
                    (max_price, years, note, group, 1 if stop_after_first else 0, domain),
                )
                self._conn.commit()
                return False
            self._conn.execute(
                """INSERT INTO domains
                   (domain, max_price, years, note, source, group_name, stop_after_first,
                    added_at, next_check_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (domain, max_price, years, note, source, group,
                 1 if stop_after_first else 0, now, now),
            )
            self._conn.commit()
            return True

    def remove_domain(self, domain: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM domains WHERE domain = ?", (domain,))
            self._conn.commit()
            return cursor.rowcount > 0

    def set_enabled(self, domain: str, enabled: bool) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE domains SET enabled = ? WHERE domain = ?", (1 if enabled else 0, domain)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def get_domain(self, domain: str) -> WatchedDomain | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM domains WHERE domain = ?", (domain,)
            ).fetchone()
        return _row_to_domain(row) if row else None

    def list_domains(self, *, enabled_only: bool = False) -> list[WatchedDomain]:
        query = "SELECT * FROM domains"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY (drop_at IS NULL), drop_at, domain"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        return [_row_to_domain(row) for row in rows]

    def due_domains(self, *, now: datetime | None = None, limit: int = 200) -> list[WatchedDomain]:
        """取出所有到点该查的域名。"""
        marker = iso(now or utcnow())
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM domains
                    WHERE enabled = 1
                      AND state != 'acquired'
                      AND (next_check_at IS NULL OR next_check_at <= ?)
                    ORDER BY (next_check_at IS NULL) DESC, next_check_at
                    LIMIT ?""",
                (marker, limit),
            ).fetchall()
        return [_row_to_domain(row) for row in rows]

    def earliest_next_check(self) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT MIN(next_check_at) AS next FROM domains
                    WHERE enabled = 1 AND state != 'acquired' AND next_check_at IS NOT NULL"""
            ).fetchone()
        return parse_datetime(row["next"]) if row and row["next"] else None

    def group_siblings(self, domain: str) -> list[WatchedDomain]:
        """取同一组里除自己之外、仍在监控的其它域名。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT group_name FROM domains WHERE domain = ?", (domain,)
            ).fetchone()
            if row is None or not row["group_name"]:
                return []
            rows = self._conn.execute(
                """SELECT * FROM domains
                    WHERE group_name = ? AND domain != ? AND enabled = 1
                      AND state != 'acquired'""",
                (row["group_name"], domain),
            ).fetchall()
        return [_row_to_domain(item) for item in rows]

    def update_domain(self, domain: str, **fields: Any) -> None:
        """按字段更新，datetime / list 自动序列化。"""
        if not fields:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if isinstance(value, datetime):
                value = iso(value)
            elif isinstance(value, (list, tuple)):
                value = _dumps(list(value))
            elif isinstance(value, (DomainState, Phase)):
                value = value.value
            elif isinstance(value, bool):
                value = 1 if value else 0
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(domain)
        with self._lock:
            self._conn.execute(
                f"UPDATE domains SET {', '.join(assignments)} WHERE domain = ?", values
            )
            self._conn.commit()

    def bump_attempts(self, domain: str, delta: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE domains SET attempts = attempts + ? WHERE domain = ?", (delta, domain)
            )
            self._conn.commit()

    def sync_config_domains(self, entries: Iterable[Any]) -> tuple[list[str], list[str]]:
        """把配置文件里的域名同步进库。

        通过 Telegram 加的域名（source='telegram'）不受配置文件删改影响，
        配置文件里删掉的 source='config' 记录会被清理。
        """
        wanted: dict[str, Any] = {entry.name: entry for entry in entries}
        added: list[str] = []
        for name, entry in wanted.items():
            if self.upsert_domain(
                name,
                max_price=entry.max_price,
                years=entry.years,
                note=entry.note,
                source="config",
                group=getattr(entry, "group", None),
                stop_after_first=getattr(entry, "stop_after_first", False),
            ):
                added.append(name)

        with self._lock:
            rows = self._conn.execute(
                "SELECT domain FROM domains WHERE source = 'config'"
            ).fetchall()
        removed = [row["domain"] for row in rows if row["domain"] not in wanted]
        for name in removed:
            self.remove_domain(name)
        return added, removed

    # -------------------------------------------------------------------- 事件

    def add_event(
        self,
        kind: str,
        *,
        domain: str | None = None,
        message: str = "",
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO events (created_at, kind, level, domain, message, data)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (iso(utcnow()), kind, level, domain, message, _dumps(data) if data else None),
            )
            self._conn.commit()

    def recent_events(self, limit: int = 20, *, domain: str | None = None) -> list[Event]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if domain:
            query += " WHERE domain = ?"
            params.append(domain)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            Event(
                kind=row["kind"],
                domain=row["domain"],
                message=row["message"],
                level=row["level"],
                created_at=parse_datetime(row["created_at"]) or utcnow(),
                data=json.loads(row["data"]) if row["data"] else None,
            )
            for row in rows
        ]

    def prune_events(self, keep: int = 5000) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """DELETE FROM events WHERE id NOT IN (
                       SELECT id FROM events ORDER BY id DESC LIMIT ?
                   )""",
                (keep,),
            )
            self._conn.commit()
            return cursor.rowcount

    # ---------------------------------------------------------------- 下单记录

    def record_purchase(self, result: RegistrationResult, *, dry_run: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO purchases
                   (created_at, domain, provider, success, order_id, price, currency, dry_run, message, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    iso(result.attempted_at),
                    result.domain,
                    result.provider,
                    1 if result.success else 0,
                    result.order_id,
                    result.price,
                    result.currency,
                    1 if dry_run else 0,
                    result.message,
                    _dumps(result.raw) if result.raw else None,
                ),
            )
            self._conn.commit()

    def spend_today(self, *, now: datetime | None = None) -> float:
        """今天（UTC）已经真实花掉的金额，dry_run 不计入。"""
        day = to_utc(now or utcnow()).strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(SUM(price), 0) AS total FROM purchases
                    WHERE success = 1 AND dry_run = 0 AND substr(created_at, 1, 10) = ?""",
                (day,),
            ).fetchone()
        return float(row["total"] or 0.0)

    def acquisitions_today(self, *, now: datetime | None = None) -> int:
        """今天（UTC）真实买成了几个。演练不计入。"""
        day = to_utc(now or utcnow()).strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM purchases
                    WHERE success = 1 AND dry_run = 0 AND substr(created_at, 1, 10) = ?""",
                (day,),
            ).fetchone()
        return int(row["n"] or 0)

    def has_successful_purchase(self, domain: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM purchases WHERE domain = ? AND success = 1 LIMIT 1", (domain,)
            ).fetchone()
        return row is not None

    def recent_purchases(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            domains = self._conn.execute(
                "SELECT state, COUNT(*) AS count FROM domains GROUP BY state"
            ).fetchall()
            totals = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled
                     FROM domains"""
            ).fetchone()
            purchases = self._conn.execute(
                """SELECT COUNT(*) AS attempts,
                          SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS wins
                     FROM purchases"""
            ).fetchone()
        return {
            "total": totals["total"] or 0,
            "enabled": totals["enabled"] or 0,
            "by_state": {row["state"]: row["count"] for row in domains},
            "purchase_attempts": purchases["attempts"] or 0,
            "purchase_wins": purchases["wins"] or 0,
            "spend_today": self.spend_today(),
            "acquired_today": self.acquisitions_today(),
        }

    # ------------------------------------------------------------------ KV 存储

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def set_kv(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _dumps(value)),
            )
            self._conn.commit()


def _row_to_domain(row: sqlite3.Row) -> WatchedDomain:
    try:
        statuses = json.loads(row["statuses"] or "[]")
    except json.JSONDecodeError:
        statuses = []
    keys = row.keys()
    return WatchedDomain(
        domain=row["domain"],
        state=DomainState(row["state"]) if row["state"] else DomainState.UNKNOWN,
        statuses=statuses if isinstance(statuses, list) else [],
        registrar=row["registrar"],
        expires_at=parse_datetime(row["expires_at"]),
        drop_at=parse_datetime(row["drop_at"]),
        pending_delete_since=parse_datetime(row["pending_delete_since"]),
        redemption_since=parse_datetime(row["redemption_since"]),
        last_checked_at=parse_datetime(row["last_checked_at"]),
        next_check_at=parse_datetime(row["next_check_at"]),
        phase=Phase(row["phase"]) if row["phase"] else Phase.IDLE,
        attempts=row["attempts"] or 0,
        max_price=row["max_price"],
        years=row["years"],
        note=row["note"],
        source=row["source"] or "config",
        group=row["group_name"] if "group_name" in keys else None,
        stop_after_first=bool(row["stop_after_first"]) if "stop_after_first" in keys else False,
        enabled=bool(row["enabled"]),
        added_at=parse_datetime(row["added_at"]) or datetime.now(timezone.utc),
        acquired_at=parse_datetime(row["acquired_at"]),
        last_error=row["last_error"] if "last_error" in keys else None,
    )
