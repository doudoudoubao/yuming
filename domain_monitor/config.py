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
from .tldgroups import merge_groups
from .utils import (
    PatternError,
    expand_env,
    expand_pattern,
    is_valid_domain,
    normalize_domain,
)


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
    # 一个健康的「已注册」域名突然 404，更可能是服务器抽风而不是真被删了。
    # 这种可疑跳变先复核一次再当真，避免误报和白跑的下单。
    reverify_available: bool = True
    reverify_delay: float = 3.0
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
    # 允许在聊天里用 /setkey 写入注册商凭据吗？
    # 默认关闭：密钥会留在 Telegram 的聊天记录里（云端存储，非端到端加密），
    # 这一条谁也消不掉。要用请自行权衡，并在写完后立刻删除那条消息。
    allow_secret_input: bool = False
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
    # 每天最多买几个。金额预算拦不住「一次性买下一堆便宜域名」——
    # 盯 58 个后缀时可能有十几个当前就是空的，开关一开会全部买走。
    max_per_day: int = 3
    # 没有单独标 auto_buy 的域名，默认自动下单吗？
    # 设成 false 就变成「白名单模式」：只有显式写了 auto_buy: true 的才会被买。
    auto_buy_default: bool = True
    # 允许在 Telegram 里切换下单模式吗？关掉的话配置文件是唯一权威，
    # 想改必须登服务器——适合「机器人只读」的用法。
    allow_remote_control: bool = True
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
    # 同一个前缀展开出来的域名归为一组；抢到组里任意一个之后
    # 可以按 stop_after_first 把其余的撤下来
    group: str | None = None
    stop_after_first: bool = False
    # 是否自动下单。None = 跟随 purchase.auto_buy_default
    auto_buy: bool | None = None


@dataclass(slots=True)
class PrefixEntry:
    """一个前缀 × 一组后缀 = 一批要盯的域名。"""

    name: str | list[str] = ""
    tlds: list[str] = field(default_factory=list)
    max_price: float | None = None
    years: int | None = None
    note: str | None = None
    # 只要抢到其中一个就够了，抢到后把同组其余的撤下来
    stop_after_first: bool = True
    # 整组是否自动下单。None = 跟随 purchase.auto_buy_default
    auto_buy: bool | None = None


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
    prefixes: list[PrefixEntry] = field(default_factory=list)
    tld_groups: dict[str, list[str]] = field(default_factory=dict)
    pattern_limit: int = 200
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
    def env_path(self) -> str:
        """密钥文件位置。跟配置文件同目录。"""
        if self.path:
            return str(Path(self.path).resolve().parent / ".env")
        return self.resolve(".env")

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


def _optional_bool(value: Any, where: str) -> bool | None:
    """三态开关：没写就是 None（跟随全局），写了就必须是布尔。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{where}.auto_buy 只能是 true / false，实际是 {value!r}")


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


def _build_domains(
    data: Any, *, limit: int = 200, groups: dict[str, list[str]] | None = None
) -> list[DomainEntry]:
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

        # 支持 mydream.{com,net,io} 这种写法
        try:
            candidates = expand_pattern(str(raw_name), limit=limit, groups=groups)
        except PatternError as exc:
            raise ConfigError(f"domains[{index}]: {exc}") from exc
        group = f"pattern:{raw_name}" if len(candidates) > 1 else None

        for candidate in candidates:
            name = normalize_domain(candidate)
            if not is_valid_domain(name):
                raise ConfigError(f"domains[{index}] 不是合法域名: {candidate}")
            if name in seen:
                continue
            seen.add(name)
            entries.append(
                DomainEntry(
                    name=name,
                    max_price=float(item["max_price"]) if item.get("max_price") is not None else None,
                    years=int(item["years"]) if item.get("years") is not None else None,
                    note=item.get("note"),
                    group=group,
                    stop_after_first=bool(item.get("stop_after_first", False)),
                    auto_buy=_optional_bool(item.get("auto_buy"), f"domains[{index}]"),
                )
            )
    return entries


def _build_prefixes(
    data: Any, *, limit: int = 200, groups: dict[str, list[str]] | None = None
) -> tuple[list[PrefixEntry], list[DomainEntry]]:
    """解析 ``prefixes:`` 段，并展开成具体的监控条目。"""
    if data is None:
        return [], []
    if not isinstance(data, list):
        raise ConfigError("prefixes 必须是列表(list)")

    prefixes: list[PrefixEntry] = []
    entries: list[DomainEntry] = []
    seen: set[str] = set()

    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ConfigError(f"prefixes[{index}] 必须是映射(mapping)")
        entry = _build(PrefixEntry, item, f"prefixes[{index}]")

        names = entry.name if isinstance(entry.name, list) else [entry.name]
        names = [str(n).strip().lower() for n in names if str(n).strip()]
        if not names:
            raise ConfigError(f"prefixes[{index}] 缺少 name（前缀）")
        if not entry.tlds:
            raise ConfigError(f"prefixes[{index}] 缺少 tlds（要盯哪些后缀）")

        # tlds 里也能写 @组名，和具体后缀混写
        tlds: list[str] = []
        for raw in entry.tlds:
            token = str(raw).strip().lower().lstrip(".")
            if not token:
                continue
            if token.startswith("@"):
                key = token[1:]
                if key not in (groups or {}):
                    available = "、".join(f"@{n}" for n in sorted(groups or {}))
                    raise ConfigError(
                        f"prefixes[{index}] 引用了不存在的后缀合集 {token}。"
                        f"可用的有：{available}"
                    )
                tlds.extend((groups or {})[key])
            else:
                tlds.append(token)
        seen_tld: set[str] = set()
        tlds = [t for t in tlds if not (t in seen_tld or seen_tld.add(t))]
        total = len(names) * len(tlds)
        if total > limit:
            raise ConfigError(
                f"prefixes[{index}] 会展开出 {total} 个域名，超过上限 {limit}，请拆小一点"
            )

        for prefix in names:
            group = f"prefix:{prefix}"
            for tld in tlds:
                name = normalize_domain(f"{prefix}.{tld}")
                if not is_valid_domain(name):
                    raise ConfigError(
                        f"prefixes[{index}] 组合出的不是合法域名: {prefix}.{tld}"
                    )
                if name in seen:
                    continue
                seen.add(name)
                entries.append(
                    DomainEntry(
                        name=name,
                        max_price=entry.max_price,
                        years=entry.years,
                        note=entry.note,
                        group=group,
                        stop_after_first=entry.stop_after_first,
                        auto_buy=entry.auto_buy,
                    )
                )
        prefixes.append(entry)
    return prefixes, entries


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
    known_top = {item.name for item in fields(AppConfig)} - {"path", "prefixes"}
    known_top.add("prefixes")
    known_top.add("tld_groups")
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
        domains=[],
        pattern_limit=int(data.get("pattern_limit", 200)),
        tld_groups=merge_groups(data.get("tld_groups")),
        path=str(path) if path else None,
    )

    # 域名和前缀都可能展开成多条，统一在这里做，共用同一个上限
    limit = config.pattern_limit
    groups = config.tld_groups
    config.domains = _build_domains(data.get("domains"), limit=limit, groups=groups)
    config.prefixes, prefix_entries = _build_prefixes(
        data.get("prefixes"), limit=limit, groups=groups
    )
    known = {item.name for item in config.domains}
    config.domains.extend(item for item in prefix_entries if item.name not in known)

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


def _validate_credentials(config: AppConfig) -> None:
    """开了真实下单，就必须把凭据填齐。

    不拦的话，域名释放那一刻才会发现「缺少必填配置」，
    而且会对着同一个错误空转上百次——窗口早就过去了。
    """
    from .registrars import PROVIDERS, env_var_name

    for index, entry in enumerate(config.registrar_configs):
        provider = (entry.provider or "dryrun").lower()
        adapter = PROVIDERS.get(provider)
        if adapter is None:
            continue

        where = f"registrars[{index}]" if config.registrars else "registrar"
        missing = [
            option for option in adapter.required_options
            if not str(entry.options.get(option) or "").strip()
        ]
        if missing:
            hints = "、".join(
                f"{option}（环境变量 {env_var_name(provider, option)}）"
                for option in missing
            )
            raise ConfigError(
                f"{where} 用的是 {provider}，但这些必填项是空的：{hints}。\n"
                f"    填法见：domain-monitor registrar {provider}\n"
                f"    还没准备好就先把 purchase.dry_run 设回 true"
            )

        if adapter.needs_contact and not entry.contact:
            raise ConfigError(
                f"{where} 用的是 {provider}，下单需要注册人资料，"
                f"但 {where}.contact 是空的。\n"
                f"    填法见：domain-monitor registrar {provider}"
            )


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
    if config.purchase.max_per_day < 1:
        raise ConfigError("purchase.max_per_day 至少为 1（设 0 请改用 enabled=false）")

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
        _validate_credentials(config)
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
