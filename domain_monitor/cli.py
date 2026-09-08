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
from .models import DomainState, PurchaseMode
from .notify.telegram import send_standalone
from .registrars import available_providers
from .tldgroups import RESTRICTED_NOTES, group_names
from .utils import (
    human_until,
    is_valid_domain,
    PatternError,
    display_domain,
    display_width,
    escape_html,
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
                print(f"✗ {raw}：不是合法域名")
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
                print(f"✗ {raw}：不是合法域名")
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

    def row(label: str, value: str, mark: str = " ") -> None:
        print(f"  {mark} {pad(label, 16)}{value}")

    print("\n【配置】")
    row("配置文件", config.path or "未找到，正在使用默认值")
    row("数据库", config.database_path)
    row("监控域名", f"{len(config.domains)} 个（写在配置文件里的）")

    async with Application(config) as app:
        print("\n【域名查询】")
        server = await app.rdap.server_for("example.com")
        row("RDAP 服务器", server or "没找到", "✓" if server else "✗")
        ok = ok and server is not None

        status = await app.rdap.lookup("example.com")
        healthy = status.state != DomainState.ERROR
        row("试查域名",
            status.summary() if healthy else f"查询失败：{status.error}",
            "✓" if healthy else "✗")
        ok = ok and healthy

        if app.probe.usable:
            probe = await app.probe.probe("example.com")
            row("DNS 探测", f"可用（example.com → {probe.value}）", "✓")
        else:
            row("DNS 探测", "不可用，冲刺会慢一些（pip install dnspython）", "!")

        print(f"\n【注册商】{len(app.pool)} 个通道")
        if all(item.name == "dryrun" for item in app.pool):
            row("提示", "当前是演练适配器，不会真的下单", "!")
            row("", "看怎么接真实注册商：domain-monitor registrar")
        for registrar, healthy, message in await app.pool.ping_all():
            # 适配器返回的消息常带 "名字: " 前缀，标签已经显示过了，去掉免得重复
            prefix = f"{registrar.name}: "
            if message.startswith(prefix):
                message = message[len(prefix):]
            row(registrar.label, message, "✓" if healthy else "✗")
            ok = ok and healthy

        print("\n【通知】")
        if config.telegram.enabled:
            me = await app.telegram.get_me()
            if me:
                row("Telegram", f"已连接 @{me.get('username')}，已发一条测试消息", "✓")
                await app.notifier.send("✅ 自检通过，Telegram 通道正常")
            else:
                row("Telegram", "连接失败，检查 bot_token 和网络", "✗")
                ok = False
        else:
            row("Telegram", "未启用", "-")

        print("\n【抢注】")
        # 必须读引擎的**生效模式**：运行时用 /mode 切过的话，
        # 配置文件里的值和实际行为可能完全相反
        purchase = app.engine.purchase
        current = app.engine.purchase_mode
        blocked = app.engine.live_blocked_reason()
        if current is PurchaseMode.MONITOR:
            row("模式", "仅监控，不会下单", "-")
        elif current is PurchaseMode.DRYRUN:
            row("模式", "演练，不会真的花钱", "-")
            if blocked:
                row("", f"（想开真实下单还差：{blocked}）", "!")
        else:
            row("模式", "⚠️  真实下单已开启", "!")
            row("单价上限", f"{purchase.max_price:.2f} {purchase.currency}")
            row("每日预算", f"{purchase.daily_budget:.2f} {purchase.currency}")

    print()
    print("  " + ("✅ 自检全部通过" if ok else "❌ 有项目没通过，见上文"))
    print()
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
            print("监控列表还是空的。用 add 加几个：")
            print("  domain-monitor add example.com")
            print("  domain-monitor add 'vps.{@two}'")
            return 0

        shown = {item.domain: display_domain(item.domain) for item in items}
        width = max(max(display_width(name) for name in shown.values()), 8) + 2
        active = sum(1 for item in items if item.enabled)
        print(f"共 {len(items)} 个域名，在盯 {active} 个\n")
        print(f"  {pad('域名', width)}{pad('状态', 14)}{pad('档位', 8)}预计释放")
        print("  " + "─" * (width + 44))
        for item in items:
            if item.drop_at:
                drop = f"{to_utc(item.drop_at):%m-%d %H:%M}  {human_until(item.drop_at)}"
            elif item.expires_at:
                drop = f"{to_utc(item.expires_at):%Y-%m-%d} 到期"
            else:
                drop = "—"
            phase = item.phase.label if item.enabled else "已停"
            print(
                f"  {pad(shown[item.domain], width)}{pad(item.state.label, 14)}"
                f"{pad(phase, 8)}{drop}"
            )
    return 0


def alert_config_error(exc: Exception, *, source: str) -> None:
    """配置炸了也要想办法通知到人。

    这时配置对象根本没解析出来，所以直接用 .env 里的
    TG_BOT_TOKEN / TG_CHAT_ID 发。发不出去就算了，不能因为报警失败再挂一次。
    """
    try:
        sent = asyncio.run(
            send_standalone(
                f"🚨 <b>域名监控启动失败</b>\n\n"
                f"{escape_html(str(exc))}\n\n"
                f"来源：<code>{escape_html(source)}</code>\n"
                f"⚠️ 服务没有在运行，现在<b>不会</b>监控任何域名。"
            )
        )
        if sent:
            print("  （已通过 Telegram 发出告警）", file=sys.stderr)
    except Exception:  # noqa: BLE001 - 报警本身不能再制造故障
        pass


def cmd_notify(message: str) -> int:
    """往 Telegram 发一条消息。

    给 systemd 的 OnFailure / ExecStopPost 用：进程都死了没法自己报信，
    只能靠外部触发。
    """
    if not message.strip():
        print("✗ 消息不能为空", file=sys.stderr)
        return 2
    ok = asyncio.run(send_standalone(message))
    if ok:
        print("✓ 已发送")
        return 0
    print("✗ 发送失败（检查 TG_BOT_TOKEN / TG_CHAT_ID 和网络）", file=sys.stderr)
    return 1


def cmd_registrar(config: AppConfig, name: str | None) -> int:
    """列出可用的注册商，或某一家的开通说明。

    本程序自己不卖域名——下单一律通过注册商的 API 完成，
    所以必须先去某一家开户、充值、开 API，再把凭据填进配置。
    """
    from .registrars import PROVIDERS, env_var_name

    if name:
        key = name.strip().lower()
        provider = PROVIDERS.get(key)
        if provider is None:
            print(f"✗ 没有 {key} 这个注册商。不带参数运行可查看全部。", file=sys.stderr)
            return 2

        print(f"\n{provider.display_name}（provider: {key}）\n")
        if provider.signup_url:
            print(f"  开户 / 开 API　{provider.signup_url}")
        print(f"  扣款方式　　　{provider.payment}")
        print(f"  支持查价　　　{'是' if provider.supports_price else '否'}")
        if provider.notes:
            print("\n  注意")
            for note in provider.notes:
                print(f"    · {note}")

        print("\n  配置写法\n")
        print("    registrar:")
        print(f"      provider: {key}")
        if provider.required_options:
            print("      options:")
            for option in provider.required_options:
                placeholder = "${" + env_var_name(key, option) + "}"
                print(f"        {option}: \"{placeholder}\"")
        if provider.needs_contact:
            print("      contact:            # 注册域名要提交的注册人资料")
            for field, sample in (
                ("first_name", "San"), ("last_name", "Zhang"),
                ("email", "you@example.com"), ("phone", "+86.13800138000"),
                ("address1", "XX 路 1 号"), ("city", "Shanghai"),
                ("state", "Shanghai"), ("postal_code", "200000"), ("country", "CN"),
            ):
                print(f"        {field}: \"{sample}\"")
        if provider.required_options:
            print("\n  密钥写进项目根目录的 .env（权限 600），不要写进 config.yaml：")
            for option in provider.required_options:
                print(f"    {env_var_name(key, option)}=你的值")
        print()
        return 0

    print("\n本程序自己不卖域名。下单是通过下面某一家的 API 完成的，")
    print("所以你需要先去其中一家开户、充值、开 API。\n")
    print(f"  {pad('provider', 12)}{pad('注册商', 18)}{pad('扣款方式', 24)}必填")
    print("  " + "─" * 74)
    for key, provider in sorted(PROVIDERS.items()):
        need = "、".join(provider.required_options) or "—"
        if provider.needs_contact:
            need += " + 联系人资料"
        print(
            f"  {pad(key, 12)}{pad(provider.display_name, 18)}"
            f"{pad(provider.payment, 24)}{need}"
        )
    current = (config.registrar_configs[0].provider or "dryrun").lower()
    print(f"\n  当前配置的是：{current}", end="")
    print("（演练适配器，不会真的下单）" if current == "dryrun" else "")
    print("\n  看某一家的详细开通说明：domain-monitor registrar namesilo\n")
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

    print("\n可用的后缀合集\n")
    for key in group_names():
        items = groups.get(key, [])
        preview = " ".join(items[:9])
        if len(items) > 9:
            preview += " …"
        mark = "⚠️" if key in RESTRICTED_NOTES else "  "
        print(f"  {mark} {pad('@' + key, 10)}{pad(f'{len(items)} 个', 7)}{preview}")
        if key in RESTRICTED_NOTES:
            print(f"     {pad('', 10)}       {RESTRICTED_NOTES[key]}")
    print("""
用法
  domain-monitor add 'vps.{@all}'      一次盯全部无门槛后缀
  domain-monitor add 'vps.{@two}'      只要两位的
  domain-monitor add 'vps.{@two,com}'  合集与具体后缀混写
  domain-monitor tlds all              看某组的完整内容
""")
    if any(key in RESTRICTED_NOTES for key in groups):
        print("⚠️ 的组有注册门槛，下单前先用 price 确认注册商卖不卖。\n")
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

class ChineseHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """argparse 自带的 usage: / options: 都是英文，这里换成中文。"""

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix or "用法：")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-monitor",
        description="域名监控与自动抢注（RDAP + DNS 探测 + Telegram）",
        formatter_class=ChineseHelpFormatter,
        epilog=(
            f"可用注册商：{'、'.join(available_providers())}\n"
            f"后缀合集：{'、'.join('@' + name for name in group_names())}"
            f"（domain-monitor tlds 可查看内容）"
        ),
        add_help=False,      # 自己加，好换成中文说明
    )
    parser.add_argument(
        "-h", "--help", action="help", help="显示这份帮助并退出"
    )
    parser.add_argument("-c", "--config", metavar="路径",
                        help="配置文件路径（默认自动找 config.yaml）")
    parser.add_argument("--database", metavar="路径", help="覆盖配置里的数据库位置")
    parser.add_argument(
        "--log-level", metavar="级别",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别"
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"%(prog)s {__version__}", help="显示版本并退出")
    parser._optionals.title = "选项"

    sub = parser.add_subparsers(dest="command", title="子命令", metavar="")
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

    registrar = sub.add_parser("registrar", help="查看注册商与开通说明")
    registrar.add_argument("name", nargs="?", help="注册商名，如 namesilo")

    notify = sub.add_parser("notify", help="往 Telegram 发一条消息（供 systemd 告警用）")
    notify.add_argument("message", nargs="+", help="消息内容")

    init = sub.add_parser("init", help="生成一份配置文件模板")
    init.add_argument("path", nargs="?", default="config.yaml")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        setup_logging("INFO")
        return cmd_init(args.path)

    if args.command == "notify":
        setup_logging("WARNING")
        load_env_files(find_config(getattr(args, "config", None)))
        return cmd_notify(" ".join(args.message))

    try:
        config = build_config(args)
    except ConfigError as exc:
        print(f"✗ 配置错误: {exc}", file=sys.stderr)
        # 常驻模式下配置炸了 = 服务起不来，必须让人知道
        if (args.command or "run") == "run":
            alert_config_error(exc, source=find_config(getattr(args, "config", None)) or "默认配置")
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
    if command == "registrar":
        return cmd_registrar(config, args.name)
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
