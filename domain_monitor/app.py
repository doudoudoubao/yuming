"""把各个组件装配成一个可运行的应用。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from .config import AppConfig
from .dnsprobe import DnsProbe
from .engine import Engine
from .models import DomainState, PurchaseMode
from .notify.telegram import Notifier, NullBot, TelegramBot, TelegramClient
from .rdap import RdapClient
from .registrars import build_registrar, credential_env_vars
from .registrars.pool import RegistrarPool
from .storage import Storage

logger = logging.getLogger(__name__)

# 重载时需要让 .env 的新值覆盖进程里的旧值。只清这些名字，
# 免得把 PATH 之类的系统变量也一起动了。
_RELOADABLE_ENV = {
    name for names in credential_env_vars().values() for name in names
} | {"TG_CHAT_ID", "TG_BOT_TOKEN", "TG_API_BASE"}


class Application:
    """持有所有长生命周期对象，负责启动与优雅关闭。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.database_path)
        self.rdap = RdapClient(config.rdap, cache_path=config.bootstrap_cache_path)
        self.pool = RegistrarPool(
            [build_registrar(item) for item in config.registrar_configs]
        )
        self.registrar = self.pool.primary
        self.telegram = TelegramClient(config.telegram)
        self.notifier = Notifier(self.telegram, config.telegram)
        self.probe = DnsProbe(config.dns)
        self.engine = Engine(
            config,
            self.storage,
            self.rdap,
            self.pool,
            self.notifier,
            probe=self.probe,
        )
        self.bot: Any = NullBot()
        if config.telegram.enabled and config.telegram.commands:
            self.bot = TelegramBot(self.telegram, config.telegram, self.engine)
        self.engine.bot = self.bot
        self.engine.reload_hook = self.reload

    async def start(self) -> None:
        await asyncio.gather(
            self.rdap.start(), self.pool.start(), self.telegram.start()
        )
        added, removed = self.storage.sync_config_domains(self.config.domains)
        if added:
            logger.info("从配置文件新增 %d 个域名: %s", len(added), ", ".join(added))
        if removed:
            logger.info("配置文件已移除 %d 个域名: %s", len(removed), ", ".join(removed))

    async def close(self) -> None:
        self.engine.stop()
        self.bot.stop()
        await asyncio.gather(
            self.rdap.close(), self.pool.close(), self.telegram.close(),
            return_exceptions=True,
        )
        self.storage.prune_events()
        self.storage.close()

    async def __aenter__(self) -> "Application":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def run_forever(self) -> None:
        """引擎 + Telegram 机器人并行跑，任一退出就整体收尾。"""
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _request_stop(signame: str) -> None:
            logger.warning("收到 %s，正在优雅退出……", signame)
            stop_event.set()
            self.engine.stop()
            self.bot.stop()

        for signame in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, _request_stop, signame)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                pass

        await self.startup_notice()

        tasks = [
            asyncio.create_task(self.engine.run(), name="engine"),
            asyncio.create_task(self.bot.run(), name="telegram"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.error("任务 %s 异常退出: %s", task.get_name(), exc)
        finally:
            self.engine.stop()
            self.bot.stop()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reload(self) -> str:
        """重新读配置和 .env，并重建注册商通道。

        改完密钥不用重启服务——否则手机上写完密钥还得去登服务器，
        这个功能就没意义了。
        """
        from .cli import load_env_files
        from .config import ConfigError, load_config

        if not self.config.path:
            return "当前没有使用配置文件，无法重载"

        # .env 里的新值要能盖掉进程里已有的旧值，否则读不到刚写的密钥
        before = dict(os.environ)
        restore = True
        try:
            for name in list(os.environ):
                if name in _RELOADABLE_ENV:
                    del os.environ[name]
            load_env_files(self.config.path)
            fresh = load_config(self.config.path)
            restore = False
        except Exception as exc:  # noqa: BLE001
            # 必须兜住所有异常：只认 ConfigError/OSError 的话，
            # YAML 语法错误会让删掉的凭据永远回不来，
            # 下一次成功重载就会带着空凭据把通道建起来
            return f"❌ 配置有误，已保持原样：{exc}"
        finally:
            if restore:
                os.environ.clear()
                os.environ.update(before)

        old_pool = self.pool
        try:
            pool = RegistrarPool(
                [build_registrar(item) for item in fresh.registrar_configs]
            )
            await pool.start()
        except Exception as exc:  # noqa: BLE001 - 重建失败要保住原来能用的通道
            logger.exception("重建注册商通道失败")
            return f"❌ 注册商配置有问题，已保持原样：{exc}"

        self.config = fresh
        self.pool = pool
        self.engine.config = fresh
        self.engine.registrar = pool
        self.engine.invalidate_mode_cache()   # 新配置可能改变模式的允许范围

        # Telegram 的配置也得换掉，否则改了 chat_id / 白名单 / 密钥输入开关
        # 之后重载会报成功，实际全是旧值
        self.telegram.config = fresh.telegram
        self.notifier.config = fresh.telegram
        if hasattr(self.bot, "config"):
            self.bot.config = fresh.telegram
        await old_pool.close()

        added, removed = self.storage.sync_config_domains(fresh.domains)
        logger.warning("配置已重载：%d 个注册商通道", len(pool))

        parts = [f"✅ 已重新加载配置", f"🏬 通道 {'、'.join(pool.labels)}"]
        if added:
            parts.append(f"➕ 新增 {len(added)} 个域名")
        if removed:
            parts.append(f"➖ 移除 {len(removed)} 个域名")
        return "\n".join(parts)

    async def startup_notice(self) -> None:
        """启动时报个到，把「接下来会不会花钱」说清楚。"""
        purchase = self.engine.purchase
        current = self.engine.purchase_mode
        count = len(self.storage.list_domains(enabled_only=True))

        if current is PurchaseMode.MONITOR:
            mode = "🔍 仅监控，不会下单"
        elif current is PurchaseMode.DRYRUN:
            mode = "🧪 演练模式，不会真的花钱"
        else:
            mode = (
                f"💸 <b>真实下单已开启</b>\n"
                f"　　每天最多 {purchase.max_per_day} 个 · "
                f"单价上限 {purchase.max_price:.0f} · 日预算 {purchase.daily_budget:.0f}"
            )
            # 真金白银模式下，先把「已经空着、开机就会被买走」的域名报出来。
            # 用户往往是盯了一批后缀之后才开开关，其中不少本来就没人注册。
            await self._warn_about_immediate_buys()

        if not self.telegram.enabled:
            return
        await self.notifier.send(
            "🚀 <b>域名监控已启动</b>\n\n"
            f"🗒 在盯 {count} 个域名\n"
            f"🏬 通道 {'、'.join(self.pool.labels)}\n"
            f"{mode}\n\n"
            "发 /help 查看用法",
            quiet=not current.spends_money,
        )

    async def _warn_about_immediate_buys(self) -> None:
        """真实下单模式启动时，提醒哪些域名会被立刻买走。"""
        # 只统计**真的会被买**的：白名单模式下大部分域名只通知不买，
        # 全报出来等于狼来了
        already = [
            item.domain
            for item in self.storage.list_domains(enabled_only=True)
            if item.state is DomainState.AVAILABLE
            and self.engine.auto_buy_allowed(item)
        ]
        if not already:
            return

        preview = "、".join(already[:8])
        if len(already) > 8:
            preview += f" 等 {len(already)} 个"
        logger.warning(
            "⚠️ 真实下单已开启，有 %d 个域名上次检查时就是可注册状态，"
            "启动后会立刻尝试买下（受每日 %d 个上限约束）：%s",
            len(already), self.engine.purchase.max_per_day, preview,
        )
        self.storage.add_event(
            "startup_pending_buys",
            message=f"启动时有 {len(already)} 个域名处于可注册状态：{preview}",
            level="warning",
        )
        if self.telegram.enabled:
            await self.notifier.send(
                f"⚠️ <b>注意</b>：有 {len(already)} 个域名现在就是可注册状态，\n"
                f"启动后会<b>立刻尝试买下</b>（每天最多 "
                f"{self.engine.purchase.max_per_day} 个）：\n"
                f"<code>{preview}</code>\n\n"
                f"不想买就先发 /pause"
            )
