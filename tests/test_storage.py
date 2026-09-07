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
