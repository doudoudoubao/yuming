"""命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .app import Application
from .config import AppConfig, ConfigError, load_config
from .models import DomainState
from .registrars import available_providers
from .tldgroups import RESTRICTED_NOTES, group_names
from .utils import (
    human_until,
    is_valid_domain,
    PatternError,
    expand_patterns,
    load_dotenv,
    normalize_domain,
    pad,
    to_utc,
)

logger = logging.getLogger("domain_monitor")

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml", "domain-monitor.yaml")


def setup_logging(level: str, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def find_config(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env = os.environ.get("DOMAIN_MONITOR_CONFIG")
    if env:
        return env
    for name in DEFAULT_CONFIG_NAMES:
        if Path(name).exists():
            return name
    return None


def load_env_files(config_path: str | None) -> None:
    """自动加载 .env，省得用户每次都要记得 source 一遍。

    先找配置文件同目录，再找当前目录。真正的环境变量优先级更高。
    """
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path).resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        count = load_dotenv(candidate)
        if count:
            logger.debug("从 %s 加载了 %d 个环境变量", candidate, count)


def build_config(args: argparse.Namespace) -> AppConfig:
    path = find_config(getattr(args, "config", None))
    load_env_files(path)
    if path is None:
        # 没有配置文件也能跑只读命令，用一份全默认配置
        config = load_config(data={})
    else:
        config = load_config(path)
    if getattr(args, "database", None):
        config.database = args.database
    if getattr(args, "log_level", None):
        config.log_level = args.log_level.upper()
    return config


# --------------------------------------------------------------------- 子命令

async def cmd_run(config: AppConfig) -> int:
    async with Application(config) as app:
        await app.run_forever()
    return 0


async def cmd_once(config: AppConfig) -> int:
    async with Application(config) as app:
        statuses = await app.engine.run_once()
        if not statuses:
            print("本轮没有到点需要检查的域名")
        for status in statuses:
            print(status.summary())
    return 0


async def cmd_check(config: AppConfig, domains: list[str]) -> int:
    exit_code = 0
    try:
        domains = expand_patterns(
            domains, limit=config.pattern_limit, groups=config.tld_groups
        )
    except PatternError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    async with Application(config) as app:
        for raw in domains:
            name = normalize_domain(raw)
            if not is_valid_domain(name):
                print(f"✗ {raw}: 不是合法域名")
                exit_code = 2
                continue
            status = await app.rdap.lookup(name)
            print(status.summary())
            if status.statuses:
                print(f"    EPP 状态: {', '.join(status.statuses)}")
            if status.nameservers:
                print(f"    NS: {', '.join(status.nameservers[:5])}")
            if status.state == DomainState.ERROR:
                exit_code = 1
    return exit_code


async def cmd_price(config: AppConfig, domains: list[str]) -> int:
    """向所有已配置的注册商通道并发查价，按价格排序输出。

    数据来自各注册商自己的 API，是**你账户的真实成交价**
    （含会员等级折扣、促销、溢价域名加价），比第三方比价站的挂牌价准。
    """
    exit_code = 0
    async with Application(config) as app:
        if not any(item.supports_price for item in app.pool):
            print(f"已配置的注册商（{'、'.join(app.pool.labels)}）都不支持查价")
            return 2

        for raw in domains:
            name = normalize_domain(raw)
            if not is_valid_domain(name):
                print(f"✗ {raw}: 不是合法域名")
                exit_code = 2
                continue

            quotes = await app.pool.compare(name)
            print(f"\n{name}")
            print(f"  {pad('注册商', 14)}{pad('可注册', 8)}{pad('价格', 16)}备注")
            print("  " + "-" * 52)
            for registrar, quote in quotes:
                avail = {True: "是", False: "否", None: "未知"}[quote.available]
                price = (
                    f"{quote.price:.2f} {quote.currency}" if quote.price is not None else "-"
                )
                notes = []
                if quote.premium:
                    notes.append("溢价域名")
                if quote.error:
                    notes.append(quote.error[:40])
                print(
                    f"  {pad(registrar.label, 14)}{pad(avail, 8)}"
                    f"{pad(price, 16)}{'; '.join(notes)}"
                )

            cheapest = await app.pool.cheapest(name, max_price=config.purchase.max_price)
            if cheapest is not None:
                registrar, quote = cheapest
                print(
                    f"  → 最便宜且在上限({config.purchase.max_price:.2f})内："
                    f"{registrar.label} {quote.price:.2f} {quote.currency}"
                )
            else:
                print("  → 没有符合价格上限的通道")
    return exit_code


async def cmd_test(config: AppConfig) -> int:
    """连通性自检：配置、RDAP、注册商、Telegram、DNS。"""
    ok = True
    print(f"配置文件      : {config.path or '（未找到，使用默认值）'}")
    print(f"数据库        : {config.database_path}")
    print(f"监控域名      : {len(config.domains)} 个（配置文件中）")

    async with Application(config) as app:
        server = await app.rdap.server_for("example.com")
        print(f"RDAP (.com)   : {server or '未找到服务器'}")
        ok = ok and server is not None

        status = await app.rdap.lookup("example.com")
        mark = "✓" if status.state != DomainState.ERROR else "✗"
        print(f"RDAP 查询     : {mark} {status.summary()}")
        ok = ok and status.state != DomainState.ERROR

        print(f"DNS 探测      : {'✓ 可用' if app.probe.usable else '✗ 不可用（未安装 dnspython 或已禁用）'}")
        if app.probe.usable:
            probe = await app.probe.probe("example.com")
            print(f"  example.com : {probe.value}")

        print(f"注册商通道    : {len(app.pool)} 个（{'、'.join(app.pool.labels)}）")
        for registrar, healthy, message in await app.pool.ping_all():
            print(f"  {registrar.label:<12}: {'✓' if healthy else '✗'} {message}")
            ok = ok and healthy

        if config.telegram.enabled:
            me = await app.telegram.get_me()
            if me:
                print(f"Telegram      : ✓ 已连接 @{me.get('username')}")
                await app.notifier.send("✅ 域名监控自检：Telegram 通道正常")
                print("                已发送一条测试消息，请查收")
            else:
                print("Telegram      : ✗ 连接失败，检查 bot_token / 网络")
                ok = False
        else:
            print("Telegram      : － 未启用")

        purchase = config.purchase
        if not purchase.enabled:
            print("抢注          : － 未启用（purchase.enabled=false）")
        elif purchase.dry_run:
            print("抢注          : 演练模式（dry_run=true，不会真的下单）")
        else:
            print(
                f"抢注          : ⚠️ 真实下单已开启！单价上限 {purchase.max_price:.2f}，"
                f"日预算 {purchase.daily_budget:.2f}"
            )
    print()
    print("自检结果      :", "✓ 全部通过" if ok else "✗ 存在问题，见上文")
    return 0 if ok else 1


async def cmd_add(config: AppConfig, domains: list[str]) -> int:
    async with Application(config) as app:
        print(await _strip_html(app.engine.cmd_add(domains)))
    return 0


async def cmd_remove(config: AppConfig, domains: list[str]) -> int:
    async with Application(config) as app:
        for name in domains:
            print(await _strip_html(app.engine.cmd_remove(name)))
    return 0


async def cmd_list(config: AppConfig) -> int:
    async with Application(config) as app:
        items = app.storage.list_domains()
        if not items:
            print("监控列表为空")
            return 0
        width = max(max(len(item.domain) for item in items), 8) + 2
        print(f"{pad('域名', width)}{pad('状态', 16)}{pad('预计释放', 22)}下次检查")
        print("-" * (width + 50))
        for item in items:
            drop = (
                f"{to_utc(item.drop_at):%Y-%m-%d %H:%M}Z" if item.drop_at else "-"
            )
            nxt = human_until(item.next_check_at) if item.next_check_at else "-"
            flag = "" if item.enabled else " (停用)"
            print(
                f"{pad(item.domain, width)}{pad(item.state.label, 16)}"
                f"{pad(drop, 22)}{nxt}{flag}"
            )
    return 0


def cmd_tlds(config: AppConfig, name: str | None) -> int:
    """列出后缀合集，或某个合集的具体内容。"""
    groups = config.tld_groups
    if name:
        key = name.strip().lstrip("@").lower()
        if key not in groups:
            print(f"✗ 没有 @{key} 这个合集。不带参数运行可查看全部。", file=sys.stderr)
            return 2
        items = groups[key]
        print(f"@{key}（{len(items)} 个后缀）")
        print("  " + " ".join(items))
        print(f"\n用法：你的前缀.{{@{key}}}")
        note = RESTRICTED_NOTES.get(key)
        if note:
            print(f"⚠️  {note}")
        return 0

    print("可用的后缀合集：\n")
    for key in group_names():
        items = groups.get(key, [])
        preview = " ".join(items[:8])
        if len(items) > 8:
            preview += f" … (共 {len(items)} 个)"
        mark = " ⚠️" if key in RESTRICTED_NOTES else ""
        print(f"  {pad('@' + key, 12)}{preview}{mark}")
    print("\n用法：")
    print("  domain_monitor add 'vps.{@two}'        一次加一批两位后缀")
    print("  domain_monitor add 'vps.{@two,com}'    合集和具体后缀混写")
    print("  domain_monitor tlds two                看某个合集的完整内容")
    if any(key in RESTRICTED_NOTES for key in groups):
        print("\n⚠️ 标记的组有注册限制，下单前先用 price 命令确认注册商是否支持。")
    return 0


async def cmd_log(config: AppConfig, limit: int) -> int:
    async with Application(config) as app:
        for event in reversed(app.storage.recent_events(limit)):
            target = f" [{event.domain}]" if event.domain else ""
            print(
                f"{to_utc(event.created_at):%Y-%m-%d %H:%M:%S}Z "
                f"{event.level.upper():<7}{target} {event.message}"
            )
    return 0


async def _strip_html(coro: object) -> str:
    """命令行里复用 Telegram 那套文案，把标签去掉即可。"""
    import re

    text = await coro  # type: ignore[misc]
    text = re.sub(r"<[^>]+>", "", str(text))
    return (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )


def cmd_init(path: str) -> int:
    target = Path(path)
    if target.exists():
        print(f"✗ {target} 已存在，不覆盖")
        return 1
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    if not example.exists():
        print(f"✗ 找不到模板 {example}")
        return 1
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"✓ 已生成配置文件 {target}")
    print("  下一步：填好 telegram / registrar 相关的环境变量，然后运行")
    print(f"  python -m domain_monitor -c {target} test")
    return 0


# ------------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-monitor",
        description="域名监控与自动抢注系统（RDAP + DNS 探测 + Telegram）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"可用注册商: {', '.join(available_providers())}",
    )
    parser.add_argument("-c", "--config", help="配置文件路径（默认找 config.yaml）")
    parser.add_argument("--database", help="覆盖配置里的数据库路径")
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别"
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动常驻监控（默认）")
    sub.add_parser("once", help="只跑一轮巡检然后退出（适合配 cron）")
    sub.add_parser("test", help="连通性自检：RDAP / 注册商 / Telegram / DNS")
    sub.add_parser("list", help="列出监控中的域名")

    check = sub.add_parser("check", help="立即查询域名状态")
    check.add_argument("domains", nargs="+")

    price = sub.add_parser("price", help="向注册商查价")
    price.add_argument("domains", nargs="+")

    add = sub.add_parser("add", help="加入监控列表")
    add.add_argument("domains", nargs="+")

    remove = sub.add_parser("rm", help="移出监控列表")
    remove.add_argument("domains", nargs="+")

    log = sub.add_parser("log", help="查看最近事件")
    log.add_argument("-n", "--limit", type=int, default=30)

    tlds = sub.add_parser("tlds", help="查看预设的后缀合集")
    tlds.add_argument("name", nargs="?", help="合集名，如 two")

    init = sub.add_parser("init", help="生成一份配置文件模板")
    init.add_argument("path", nargs="?", default="config.yaml")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        setup_logging("INFO")
        return cmd_init(args.path)

    try:
        config = build_config(args)
    except ConfigError as exc:
        print(f"✗ 配置错误: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_level, config.log_file)

    command = args.command or "run"
    runners = {
        "run": lambda: cmd_run(config),
        "once": lambda: cmd_once(config),
        "test": lambda: cmd_test(config),
        "list": lambda: cmd_list(config),
        "check": lambda: cmd_check(config, args.domains),
        "price": lambda: cmd_price(config, args.domains),
        "add": lambda: cmd_add(config, args.domains),
        "rm": lambda: cmd_remove(config, args.domains),
        "log": lambda: cmd_log(config, args.limit),
    }
    # 同步子命令单独处理，不用绕 asyncio
    if command == "tlds":
        return cmd_tlds(config, args.name)
    runner = runners.get(command)
    if runner is None:  # pragma: no cover - argparse 已经拦住了
        parser.print_help()
        return 2

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"✗ 配置错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
