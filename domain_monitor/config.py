"""配置加载与校验。

配置文件是 YAML，所有值都支持 ``${ENV_VAR}`` / ``${ENV_VAR:-默认值}`` 展开，
密钥建议只写在环境变量里，不要落到仓库。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .models import LifecycleProfile
from .utils import expand_env, is_valid_domain, normalize_domain


class ConfigError(Exception):
    """配置不合法。"""


@dataclass(slots=True)
class PollConfig:
    """轮询节奏（秒）。"""

    idle_interval: float = 21600.0    # 常规巡检：6 小时
    watch_interval: float = 1800.0    # 已进入删除流程：30 分钟
    near_interval: float = 60.0       # 临近释放：1 分钟
    sprint_interval: float = 1.0      # 冲刺：1 秒
    watch_lead: float = 259200.0      # 距释放 3 天进入 watch
    near_lead: float = 7200.0         # 距释放 2 小时进入 near
    sprint_lead: float = 300.0        # 距释放 5 分钟进入 sprint
    sprint_tail: float = 5400.0       # 释放时间之后再冲 90 分钟
    jitter: float = 0.15              # 间隔抖动比例
    concurrency: int = 8              # 常规查询并发
    error_backoff: float = 300.0      # 查询失败后的最小重试间隔


@dataclass(slots=True)
class RdapConfig:
    bootstrap_url: str = "https://data.iana.org/rdap/dns.json"
    # bootstrap 拉不到 / 后缀没收录时的通用兜底：rdap.org 会按 IANA 表 302 到正主
    fallback_service: str = "https://rdap.org/"
    bootstrap_ttl: float = 86400.0
    bootstrap_cache: str = "rdap-bootstrap.json"
    timeout: float = 15.0
    rps_per_host: float = 1.0
    burst_per_host: float = 3.0
    max_retries: int = 2
    user_agent: str = "domain-monitor/1.0 (+https://github.com/doudoudoubao/yuming)"
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DnsConfig:
    """冲刺阶段的廉价预探测。

    直接问 TLD 权威服务器要目标域名的 NS 记录：
    NXDOMAIN 说明注册局里已经没有这条委派了，是域名被删除的强信号。
    注意它只是「信号」——注册着但没设 NS 的域名同样返回 NXDOMAIN，
    所以真正下单前还要看 RDAP 或者直接让注册商 API 去判定。
    """

    enabled: bool = True
    timeout: float = 2.0
    interval: float = 0.5     # 冲刺阶段 DNS 探测间隔
    resolvers: list[str] = field(default_factory=list)  # 留空则用 TLD 权威服务器
    cache_ttl: float = 3600.0  # TLD NS 缓存时长


@dataclass(slots=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    commands: bool = True             # 是否开启 /add /list 等交互命令
    poll_timeout: int = 30            # getUpdates long polling 秒数
    api_base: str = "https://api.telegram.org"
    timeout: float = 20.0
    silent_idle: bool = True          # 状态没变化时不推送
    notify_states: list[str] = field(
        default_factory=lambda: ["expired", "redemption", "pending_delete", "available", "acquired"]
    )


@dataclass(slots=True)
class PurchaseConfig:
    enabled: bool = False             # 总开关，默认关闭，防止误下单
    dry_run: bool = True              # 只走流程不真的付钱
    years: int = 1
    max_price: float = 50.0           # 单域名价格上限
    daily_budget: float = 200.0       # 每日累计花费上限
    currency: str = "USD"
    max_attempts: int = 120           # 单个域名单次冲刺的最大下单次数
    attempt_interval: float = 0.5     # 两次下单之间的间隔
    attempt_concurrency: int = 3      # 并发下单通道数
    attempt_window: float = 900.0     # 冲刺下单最长持续时间
    check_price_first: bool = True    # 下单前先查价（注册商支持时）
    compare_prices: bool = True       # 配了多个注册商时，比价后挑最便宜的下单
    parallel_registrars: bool = True  # 冲刺时同时向所有通道下单（先成功者胜）
    skip_rdap_confirm: bool = True    # 冲刺时跳过 RDAP 复核，直接下单抢时间
    confirm_via_telegram: bool = False  # 下单前要 TG 点确认（会慢几秒）
    confirm_timeout: float = 60.0
    whois_privacy: bool = True
    auto_renew: bool = False
    nameservers: list[str] = field(default_factory=list)
    stop_after_success: bool = True   # 抢到后从监控列表里摘掉


@dataclass(slots=True)
class RegistrarConfig:
    provider: str = "dryrun"
    options: dict[str, Any] = field(default_factory=dict)
    contact: dict[str, Any] = field(default_factory=dict)
    timeout: float = 20.0


@dataclass(slots=True)
class LifecycleConfig:
    default: LifecycleProfile = field(default_factory=LifecycleProfile)
    tlds: dict[str, LifecycleProfile] = field(default_factory=dict)

    def profile_for(self, tld: str) -> LifecycleProfile:
        return self.tlds.get((tld or "").lower(), self.default)


@dataclass(slots=True)
class DomainEntry:
    name: str
    max_price: float | None = None
    years: int | None = None
    note: str | None = None


@dataclass(slots=True)
class AppConfig:
    database: str = "domain-monitor.db"
    log_level: str = "INFO"
    log_file: str | None = None
    state_dir: str = "."
    poll: PollConfig = field(default_factory=PollConfig)
    rdap: RdapConfig = field(default_factory=RdapConfig)
    dns: DnsConfig = field(default_factory=DnsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    purchase: PurchaseConfig = field(default_factory=PurchaseConfig)
    registrar: RegistrarConfig = field(default_factory=RegistrarConfig)
    registrars: list[RegistrarConfig] = field(default_factory=list)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    domains: list[DomainEntry] = field(default_factory=list)
    path: str | None = None

    def resolve(self, filename: str) -> str:
        """相对路径按 state_dir 解析。"""
        candidate = Path(filename)
        if candidate.is_absolute():
            return str(candidate)
        return str(Path(self.state_dir) / candidate)

    @property
    def database_path(self) -> str:
        return self.resolve(self.database)

    @property
    def bootstrap_cache_path(self) -> str:
        return self.resolve(self.rdap.bootstrap_cache)

    @property
    def registrar_configs(self) -> list[RegistrarConfig]:
        """实际使用的注册商列表。

        配了 ``registrars:``（复数）就用它，否则退回单个 ``registrar:``，
        这样老配置文件不用改也能跑。
        """
        return list(self.registrars) if self.registrars else [self.registrar]


# 常见 gTLD 的删除周期。数值来自 ICANN 的到期恢复政策（ERRP）：
# 到期后最多 45 天自动续费期，30 天赎回期，5 天 pendingDelete。
# drop_window 是社区长期观测到的经验窗口，各注册局并不承诺，
# 只用来决定「什么时候开始高频探测」，不影响正确性。
_BUILTIN_LIFECYCLE: dict[str, dict[str, Any]] = {
    "com": {"drop_window_start": "17:30", "drop_window_end": "20:30"},
    "net": {"drop_window_start": "17:30", "drop_window_end": "20:30"},
    "org": {"drop_window_start": "17:00", "drop_window_end": "20:00"},
    "info": {"drop_window_start": "17:00", "drop_window_end": "20:00"},
    "biz": {"drop_window_start": "17:00", "drop_window_end": "20:00"},
    "cn": {"grace_days": 30.0, "redemption_days": 30.0, "pending_delete_days": 5.0},
    "io": {"grace_days": 30.0, "redemption_days": 30.0, "pending_delete_days": 5.0},
    "co": {"grace_days": 30.0, "redemption_days": 30.0, "pending_delete_days": 5.0},
    "me": {"grace_days": 30.0, "redemption_days": 30.0, "pending_delete_days": 5.0},
    "xyz": {},
    "dev": {},
    "app": {},
}


def _build(cls: type, data: Any, where: str) -> Any:
    """把 dict 填进 dataclass，顺便做未知键 / 类型检查。"""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"{where} 必须是映射(mapping)，实际是 {type(data).__name__}")
    known = {item.name for item in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where} 存在未知配置项: {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for item in fields(cls):
        if item.name not in data:
            continue
        value = data[item.name]
        if is_dataclass(item.type) and isinstance(value, dict):
            value = _build(item.type, value, f"{where}.{item.name}")
        kwargs[item.name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:  # pragma: no cover - dataclass 自己的报错
        raise ConfigError(f"{where}: {exc}") from exc


def _coerce_numbers(obj: Any, cls: type, where: str) -> None:
    """YAML 里 ``60`` 是 int，dataclass 想要 float，这里顺手转一下并做范围校验。"""
    for item in fields(cls):
        value = getattr(obj, item.name, None)
        if isinstance(value, bool) or value is None:
            continue
        if item.type in ("float", float) and isinstance(value, (int, float)):
            setattr(obj, item.name, float(value))
            if float(value) < 0:
                raise ConfigError(f"{where}.{item.name} 不能为负数")
        elif item.type in ("int", int) and isinstance(value, (int, float)):
            setattr(obj, item.name, int(value))


def _build_lifecycle(data: Any) -> LifecycleConfig:
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError("lifecycle 必须是映射(mapping)")
    default = _build(LifecycleProfile, data.get("default"), "lifecycle.default")

    tlds: dict[str, LifecycleProfile] = {}
    merged: dict[str, dict[str, Any]] = {
        key: dict(value) for key, value in _BUILTIN_LIFECYCLE.items()
    }
    for key, value in (data.get("tlds") or {}).items():
        if not isinstance(value, dict):
            raise ConfigError(f"lifecycle.tlds.{key} 必须是映射(mapping)")
        merged.setdefault(str(key).lower().lstrip("."), {}).update(value)

    for key, value in merged.items():
        base = copy.deepcopy(default)
        for name, item in value.items():
            if not hasattr(base, name):
                raise ConfigError(f"lifecycle.tlds.{key} 存在未知配置项: {name}")
            setattr(base, name, item)
        tlds[key] = base
    return LifecycleConfig(default=default, tlds=tlds)


def _build_registrars(data: Any) -> list[RegistrarConfig]:
    """解析 ``registrars:`` 列表（多通道比价 / 并发抢注用）。"""
    if data is None:
        return []
    if not isinstance(data, list):
        raise ConfigError("registrars 必须是列表(list)")
    entries: list[RegistrarConfig] = []
    for index, item in enumerate(data):
        entries.append(_build(RegistrarConfig, item, f"registrars[{index}]"))
    return entries


def _build_domains(data: Any) -> list[DomainEntry]:
    entries: list[DomainEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(data or []):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise ConfigError(f"domains[{index}] 必须是字符串或映射(mapping)")
        raw_name = item.get("name") or item.get("domain")
        if not raw_name:
            raise ConfigError(f"domains[{index}] 缺少 name 字段")
        name = normalize_domain(str(raw_name))
        if not is_valid_domain(name):
            raise ConfigError(f"domains[{index}] 不是合法域名: {raw_name}")
        if name in seen:
            continue
        seen.add(name)
        entries.append(
            DomainEntry(
                name=name,
                max_price=float(item["max_price"]) if item.get("max_price") is not None else None,
                years=int(item["years"]) if item.get("years") is not None else None,
                note=item.get("note"),
            )
        )
    return entries


def load_config(path: str | Path | None = None, *, data: dict[str, Any] | None = None) -> AppConfig:
    """从 YAML 文件（或直接从 dict）加载配置。"""
    if data is None:
        if path is None:
            raise ConfigError("必须提供配置文件路径")
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigError(f"配置文件不存在: {file_path}")
        import yaml  # 延迟导入，方便只跑单测的场景

        loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError("配置文件顶层必须是映射(mapping)")
        data = loaded

    data = expand_env(copy.deepcopy(data))
    known_top = {item.name for item in fields(AppConfig)} - {"path"}
    unknown = set(data) - known_top
    if unknown:
        raise ConfigError(f"顶层存在未知配置项: {', '.join(sorted(unknown))}")

    config = AppConfig(
        database=data.get("database", AppConfig.database),
        log_level=str(data.get("log_level", AppConfig.log_level)).upper(),
        log_file=data.get("log_file"),
        state_dir=data.get("state_dir", AppConfig.state_dir),
        poll=_build(PollConfig, data.get("poll"), "poll"),
        rdap=_build(RdapConfig, data.get("rdap"), "rdap"),
        dns=_build(DnsConfig, data.get("dns"), "dns"),
        telegram=_build(TelegramConfig, data.get("telegram"), "telegram"),
        purchase=_build(PurchaseConfig, data.get("purchase"), "purchase"),
        registrar=_build(RegistrarConfig, data.get("registrar"), "registrar"),
        registrars=_build_registrars(data.get("registrars")),
        lifecycle=_build_lifecycle(data.get("lifecycle")),
        domains=_build_domains(data.get("domains")),
        path=str(path) if path else None,
    )

    for section, name in (
        (config.poll, "poll"),
        (config.rdap, "rdap"),
        (config.dns, "dns"),
        (config.purchase, "purchase"),
        (config.telegram, "telegram"),
    ):
        _coerce_numbers(section, type(section), name)

    _validate(config)
    return config


def _validate(config: AppConfig) -> None:
    if config.poll.concurrency < 1:
        raise ConfigError("poll.concurrency 至少为 1")
    if config.poll.sprint_interval <= 0:
        raise ConfigError("poll.sprint_interval 必须大于 0")
    if not 0 <= config.poll.jitter < 1:
        raise ConfigError("poll.jitter 取值范围是 [0, 1)")
    if config.rdap.rps_per_host <= 0:
        raise ConfigError("rdap.rps_per_host 必须大于 0")
    if config.purchase.years < 1:
        raise ConfigError("purchase.years 至少为 1")
    if config.purchase.attempt_concurrency < 1:
        raise ConfigError("purchase.attempt_concurrency 至少为 1")

    if config.telegram.enabled:
        if not config.telegram.bot_token:
            raise ConfigError("telegram.enabled=true 时必须提供 bot_token（建议用环境变量）")
        if not config.telegram.chat_id and not config.telegram.allowed_user_ids:
            raise ConfigError("telegram 需要配置 chat_id 或 allowed_user_ids 之一")

    # 真金白银的开关：非 dry_run 时把该拦的都拦住
    if config.purchase.enabled and not config.purchase.dry_run:
        fake = [
            item.provider or "dryrun"
            for item in config.registrar_configs
            if (item.provider or "dryrun") in ("", "dryrun")
        ]
        if fake:
            where = "registrars 列表里" if config.registrars else "registrar.provider"
            raise ConfigError(
                f"purchase.dry_run=false 时必须配置真实的注册商，但 {where} 仍是 dryrun 假适配器"
            )
        if config.purchase.max_price <= 0:
            raise ConfigError("purchase.max_price 必须大于 0，避免无上限下单")
        if config.purchase.daily_budget <= 0:
            raise ConfigError("purchase.daily_budget 必须大于 0，避免无上限下单")

    for key, profile in config.lifecycle.tlds.items():
        for attr in ("drop_window_start", "drop_window_end"):
            value = getattr(profile, attr)
            if value is None:
                continue
            try:
                hour, minute = (int(part) for part in str(value).split(":", 1))
            except ValueError as exc:
                raise ConfigError(f"lifecycle.tlds.{key}.{attr} 格式应为 HH:MM") from exc
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ConfigError(f"lifecycle.tlds.{key}.{attr} 不是合法时间")
