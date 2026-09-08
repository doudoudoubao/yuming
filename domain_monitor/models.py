"""数据模型：域名状态、查询结果、注册结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .utils import display_domain, iso, to_utc, utcnow


class DomainState(str, Enum):
    """域名生命周期状态。

    gTLD 的典型删除流程::

        到期 → 续费宽限期(0~45天) → 赎回期(30天) → pendingDelete(5天) → 释放
    """

    UNKNOWN = "unknown"           # 还没查过 / 查询失败
    REGISTERED = "registered"     # 正常注册中
    EXPIRED = "expired"           # 已过期，处于续费宽限期
    REDEMPTION = "redemption"     # 赎回期
    PENDING_DELETE = "pending_delete"  # 待删除，5 天后释放
    AVAILABLE = "available"       # 未注册，可以抢
    ACQUIRED = "acquired"         # 已被本系统注册成功
    ERROR = "error"               # 查询异常

    @property
    def label(self) -> str:
        return _STATE_LABELS.get(self, self.value)

    @property
    def emoji(self) -> str:
        return _STATE_EMOJI.get(self, "•")

    @property
    def is_droppable(self) -> bool:
        """是否处于「快要掉了」的阶段。"""
        return self in (DomainState.EXPIRED, DomainState.REDEMPTION, DomainState.PENDING_DELETE)


_STATE_LABELS = {
    DomainState.UNKNOWN: "未知",
    DomainState.REGISTERED: "已注册",
    DomainState.EXPIRED: "已过期(宽限期)",
    DomainState.REDEMPTION: "赎回期",
    DomainState.PENDING_DELETE: "待删除",
    DomainState.AVAILABLE: "可注册",
    DomainState.ACQUIRED: "已抢注",
    DomainState.ERROR: "查询失败",
}

_STATE_EMOJI = {
    DomainState.UNKNOWN: "❔",
    DomainState.REGISTERED: "🔒",
    DomainState.EXPIRED: "⏰",
    DomainState.REDEMPTION: "🩹",
    DomainState.PENDING_DELETE: "🔥",
    DomainState.AVAILABLE: "🟢",
    DomainState.ACQUIRED: "🎉",
    DomainState.ERROR: "⚠️",
}


class Phase(str, Enum):
    """轮询节奏档位。"""

    IDLE = "idle"        # 常规巡检
    WATCH = "watch"      # 已进入删除流程，加密监控
    NEAR = "near"        # 临近预测释放时间
    SPRINT = "sprint"    # 冲刺，高频探测 + 直接下单

    @property
    def label(self) -> str:
        return _PHASE_LABELS[self]

    @property
    def emoji(self) -> str:
        return _PHASE_EMOJI[self]


_PHASE_LABELS = {
    Phase.IDLE: "常规",
    Phase.WATCH: "盯紧",
    Phase.NEAR: "临近",
    Phase.SPRINT: "冲刺",
}

_PHASE_EMOJI = {
    Phase.IDLE: "🌙",
    Phase.WATCH: "👀",
    Phase.NEAR: "⏱",
    Phase.SPRINT: "🔥",
}

# 域名是怎么进到监控列表里的
SOURCE_LABELS = {
    "config": "配置文件",
    "telegram": "Telegram",
    "cli": "命令行",
}


def source_label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or "", source or "未知")


@dataclass(slots=True)
class DomainStatus:
    """一次查询得到的域名状态快照。"""

    domain: str
    state: DomainState = DomainState.UNKNOWN
    statuses: list[str] = field(default_factory=list)
    registrar: str | None = None
    nameservers: list[str] = field(default_factory=list)
    registered_at: datetime | None = None
    expires_at: datetime | None = None
    changed_at: datetime | None = None
    checked_at: datetime = field(default_factory=utcnow)
    source: str = "rdap"
    error: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        return self.state == DomainState.AVAILABLE

    def summary(self) -> str:
        parts = [f"{self.state.emoji} {display_domain(self.domain)} {self.state.label}"]
        if self.registrar:
            parts.append(f"注册商={self.registrar}")
        if self.expires_at:
            parts.append(f"到期={to_utc(self.expires_at):%Y-%m-%d}")
        if self.error:
            parts.append(f"错误={self.error}")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "state": self.state.value,
            "statuses": list(self.statuses),
            "registrar": self.registrar,
            "nameservers": list(self.nameservers),
            "registered_at": iso(self.registered_at),
            "expires_at": iso(self.expires_at),
            "changed_at": iso(self.changed_at),
            "checked_at": iso(self.checked_at),
            "source": self.source,
            "error": self.error,
        }


@dataclass(slots=True)
class WatchedDomain:
    """监控列表中的一条记录（持久化在 SQLite 里）。"""

    domain: str
    state: DomainState = DomainState.UNKNOWN
    statuses: list[str] = field(default_factory=list)
    registrar: str | None = None
    expires_at: datetime | None = None
    drop_at: datetime | None = None
    pending_delete_since: datetime | None = None
    redemption_since: datetime | None = None
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    phase: Phase = Phase.IDLE
    attempts: int = 0
    max_price: float | None = None
    years: int | None = None
    note: str | None = None
    source: str = "config"
    group: str | None = None
    stop_after_first: bool = False
    auto_buy: bool | None = None      # None = 跟随全局默认
    enabled: bool = True
    added_at: datetime = field(default_factory=utcnow)
    acquired_at: datetime | None = None
    last_error: str | None = None

    def seconds_to_drop(self, *, now: datetime | None = None) -> float | None:
        if self.drop_at is None:
            return None
        return (to_utc(self.drop_at) - (now or utcnow())).total_seconds()


@dataclass(slots=True)
class Availability:
    """注册商返回的可注册性与价格。"""

    domain: str
    available: bool | None = None
    price: float | None = None
    currency: str = "USD"
    premium: bool = False
    raw: dict[str, Any] | None = None
    error: str | None = None


@dataclass(slots=True)
class RegistrationResult:
    """一次下单尝试的结果。"""

    domain: str
    success: bool
    provider: str = ""
    order_id: str | None = None
    price: float | None = None
    currency: str = "USD"
    message: str = ""
    retryable: bool = True
    raw: dict[str, Any] | None = None
    attempted_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "success": self.success,
            "provider": self.provider,
            "order_id": self.order_id,
            "price": self.price,
            "currency": self.currency,
            "message": self.message,
            "retryable": self.retryable,
            "attempted_at": iso(self.attempted_at),
        }


@dataclass(slots=True)
class Event:
    """事件流记录，用于 /log 与审计。"""

    kind: str
    domain: str | None = None
    message: str = ""
    level: str = "info"
    created_at: datetime = field(default_factory=utcnow)
    data: dict[str, Any] | None = None


# RDAP / EPP 状态码归一化：RDAP 用空格，WHOIS 用驼峰，这里统一成小写下划线形式。
def normalize_status(value: str) -> str:
    return "_".join(
        "".join(
            f"_{char.lower()}" if char.isupper() else char for char in str(value).strip()
        )
        .replace("-", " ")
        .replace("_", " ")
        .split()
    )


PENDING_DELETE_STATUSES = {"pending_delete", "pending_delete_restorable", "redemption_period_ended"}
REDEMPTION_STATUSES = {"redemption_period", "pending_restore"}
EXPIRED_STATUSES = {"auto_renew_period", "renew_period", "expired", "pending_renew"}
INACTIVE_STATUSES = {"inactive", "client_hold", "server_hold"}


def classify_statuses(statuses: list[str], expires_at: datetime | None = None) -> DomainState:
    """根据 EPP 状态码集合判断域名处于哪个生命周期阶段。"""
    normalized = {normalize_status(item) for item in statuses}
    if normalized & PENDING_DELETE_STATUSES:
        return DomainState.PENDING_DELETE
    if normalized & REDEMPTION_STATUSES:
        return DomainState.REDEMPTION
    if normalized & EXPIRED_STATUSES:
        return DomainState.EXPIRED
    if expires_at is not None and to_utc(expires_at) < utcnow():
        return DomainState.EXPIRED
    return DomainState.REGISTERED


@dataclass(slots=True)
class LifecycleProfile:
    """某个 TLD 的删除周期参数，用于预测释放时间。"""

    grace_days: float = 45.0        # 续费宽限期（自动续费期）
    redemption_days: float = 30.0   # 赎回期
    pending_delete_days: float = 5.0  # 待删除期
    drop_window_start: str | None = None  # UTC "HH:MM"
    drop_window_end: str | None = None

    def total_days(self) -> float:
        return self.grace_days + self.redemption_days + self.pending_delete_days


def estimate_drop_time(
    state: DomainState,
    profile: LifecycleProfile,
    *,
    expires_at: datetime | None = None,
    pending_delete_since: datetime | None = None,
    redemption_since: datetime | None = None,
) -> datetime | None:
    """预测域名释放时间。

    优先级：观测到的 pendingDelete 起点 > 赎回期起点 > 到期时间外推。
    越靠后的推算越粗，只用来决定什么时候提高轮询频率。
    """
    if state == DomainState.PENDING_DELETE and pending_delete_since is not None:
        return to_utc(pending_delete_since) + timedelta(days=profile.pending_delete_days)
    if state == DomainState.REDEMPTION and redemption_since is not None:
        return to_utc(redemption_since) + timedelta(
            days=profile.redemption_days + profile.pending_delete_days
        )
    if expires_at is not None:
        return to_utc(expires_at) + timedelta(days=profile.total_days())
    return None
