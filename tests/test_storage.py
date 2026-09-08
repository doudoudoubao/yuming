from datetime import timedelta

from domain_monitor.config import DomainEntry
from domain_monitor.models import DomainState, Phase, RegistrationResult
from domain_monitor.storage import Storage
from domain_monitor.utils import utcnow


def test_upsert_reports_new_vs_existing(storage: Storage):
    assert storage.upsert_domain("a.com") is True
    assert storage.upsert_domain("a.com") is False


def test_upsert_updates_without_clobbering(storage: Storage):
    storage.upsert_domain("a.com", max_price=10, note="first")
    storage.upsert_domain("a.com", max_price=20)
    item = storage.get_domain("a.com")
    assert item.max_price == 20
    assert item.note == "first"  # 未提供的字段保持原值


def test_config_sync_preserves_telegram_domains(storage: Storage):
    storage.upsert_domain("from-config.com", source="config")
    storage.upsert_domain("from-tg.com", source="telegram")

    added, removed = storage.sync_config_domains([DomainEntry(name="new.com")])

    assert added == ["new.com"]
    assert removed == ["from-config.com"]
    names = {item.domain for item in storage.list_domains()}
    assert names == {"from-tg.com", "new.com"}


def test_due_domains_respects_schedule(storage: Storage):
    now = utcnow()
    storage.upsert_domain("soon.com")
    storage.upsert_domain("later.com")
    storage.update_domain("soon.com", next_check_at=now - timedelta(seconds=5))
    storage.update_domain("later.com", next_check_at=now + timedelta(hours=1))

    due = [item.domain for item in storage.due_domains(now=now)]
    assert due == ["soon.com"]


def test_due_domains_skips_disabled_and_acquired(storage: Storage):
    storage.upsert_domain("off.com")
    storage.upsert_domain("won.com")
    storage.set_enabled("off.com", False)
    storage.update_domain("won.com", state=DomainState.ACQUIRED)
    assert storage.due_domains() == []


def test_round_trip_types(storage: Storage):
    now = utcnow()
    storage.upsert_domain("a.com")
    storage.update_domain(
        "a.com",
        state=DomainState.PENDING_DELETE,
        phase=Phase.SPRINT,
        statuses=["pending delete", "client hold"],
        drop_at=now,
        enabled=True,
    )
    item = storage.get_domain("a.com")
    assert item.state is DomainState.PENDING_DELETE
    assert item.phase is Phase.SPRINT
    assert item.statuses == ["pending delete", "client hold"]
    assert abs((item.drop_at - now).total_seconds()) < 1
    assert item.enabled is True


def test_spend_today_excludes_dry_run(storage: Storage):
    storage.record_purchase(
        RegistrationResult(domain="a.com", success=True, price=10.0), dry_run=False
    )
    storage.record_purchase(
        RegistrationResult(domain="b.com", success=True, price=99.0), dry_run=True
    )
    storage.record_purchase(
        RegistrationResult(domain="c.com", success=False, price=50.0), dry_run=False
    )
    assert storage.spend_today() == 10.0


def test_has_successful_purchase(storage: Storage):
    storage.record_purchase(RegistrationResult(domain="a.com", success=False), dry_run=False)
    assert storage.has_successful_purchase("a.com") is False
    storage.record_purchase(RegistrationResult(domain="a.com", success=True), dry_run=False)
    assert storage.has_successful_purchase("a.com") is True


def test_events_and_pruning(storage: Storage):
    for index in range(10):
        storage.add_event("tick", domain="a.com", message=f"e{index}")
    storage.add_event("other", domain="b.com", message="x")

    assert len(storage.recent_events(5)) == 5
    assert storage.recent_events(50, domain="b.com")[0].message == "x"
    storage.prune_events(keep=3)
    assert len(storage.recent_events(50)) == 3


def test_kv_roundtrip(storage: Storage):
    assert storage.get_kv("missing", "default") == "default"
    storage.set_kv("flag", True)
    assert storage.get_kv("flag") is True
    storage.set_kv("obj", {"a": [1, 2]})
    assert storage.get_kv("obj") == {"a": [1, 2]}


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "state.db"
    with Storage(path) as first:
        first.upsert_domain("a.com", max_price=15)
        first.update_domain("a.com", state=DomainState.REDEMPTION)
    with Storage(path) as second:
        item = second.get_domain("a.com")
        assert item.state is DomainState.REDEMPTION
        assert item.max_price == 15


def test_stats(storage: Storage):
    storage.upsert_domain("a.com")
    storage.upsert_domain("b.com")
    storage.update_domain("b.com", state=DomainState.PENDING_DELETE)
    storage.record_purchase(
        RegistrationResult(domain="b.com", success=True, price=8.0), dry_run=False
    )
    stats = storage.stats()
    assert stats["total"] == 2
    assert stats["by_state"]["pending_delete"] == 1
    assert stats["purchase_wins"] == 1
    assert stats["spend_today"] == 8.0


def test_legacy_database_is_migrated(tmp_path):
    """已有用户升级时不能撞上 no such column。"""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE domains (
            domain TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'unknown',
            statuses TEXT NOT NULL DEFAULT '[]', registrar TEXT, expires_at TEXT,
            drop_at TEXT, pending_delete_since TEXT, last_checked_at TEXT,
            next_check_at TEXT, phase TEXT NOT NULL DEFAULT 'idle',
            attempts INTEGER NOT NULL DEFAULT 0, max_price REAL, years INTEGER,
            note TEXT, source TEXT NOT NULL DEFAULT 'config',
            enabled INTEGER NOT NULL DEFAULT 1, added_at TEXT NOT NULL,
            acquired_at TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, kind TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info', domain TEXT,
            message TEXT NOT NULL DEFAULT '', data TEXT);
        CREATE TABLE purchases (id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, domain TEXT NOT NULL, provider TEXT NOT NULL,
            success INTEGER NOT NULL, order_id TEXT, price REAL, currency TEXT,
            dry_run INTEGER NOT NULL DEFAULT 0, message TEXT, raw TEXT);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute(
        "INSERT INTO domains (domain, state, added_at, max_price) "
        "VALUES ('legacy.com', 'registered', '2026-01-01T00:00:00+00:00', 42.0)"
    )
    conn.commit()
    conn.close()

    with Storage(path) as store:
        item = store.get_domain("legacy.com")
        assert item.max_price == 42.0            # 旧数据完好
        assert item.group is None                # 新列有默认值
        assert item.stop_after_first is False
        store.upsert_domain("new.com", group="prefix:x", stop_after_first=True)
        assert store.get_domain("new.com").stop_after_first is True

    # 幂等：再开一次不该出错
    with Storage(path) as store:
        assert store.get_domain("legacy.com") is not None


def test_auto_buy_roundtrip(storage: Storage):
    storage.upsert_domain("a.com", auto_buy=True)
    storage.upsert_domain("b.com", auto_buy=False)
    storage.upsert_domain("c.com")

    assert storage.get_domain("a.com").auto_buy is True
    assert storage.get_domain("b.com").auto_buy is False
    assert storage.get_domain("c.com").auto_buy is None      # 跟随全局


def test_set_auto_buy_toggles_and_resets(storage: Storage):
    storage.upsert_domain("a.com")

    assert storage.set_auto_buy("a.com", True) is True
    assert storage.get_domain("a.com").auto_buy is True

    storage.set_auto_buy("a.com", False)
    assert storage.get_domain("a.com").auto_buy is False

    storage.set_auto_buy("a.com", None)                      # 恢复跟随全局
    assert storage.get_domain("a.com").auto_buy is None

    assert storage.set_auto_buy("missing.com", True) is False


def test_upsert_does_not_clobber_auto_buy(storage: Storage):
    """配置同步时没提供 auto_buy，不该把已有的设置抹掉。"""
    storage.upsert_domain("a.com", auto_buy=False)
    storage.upsert_domain("a.com", max_price=20)

    assert storage.get_domain("a.com").auto_buy is False
    assert storage.get_domain("a.com").max_price == 20
