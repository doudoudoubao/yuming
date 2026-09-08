"""把各个组件装配成一个可运行的应用。"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from .config import AppConfig
from .dnsprobe import DnsProbe
from .engine import Engine
from .models import DomainState, PurchaseMode
from .notify.telegram import Notifier, NullBot, TelegramBot, TelegramClient
from .rdap import RdapClient
from .registrars import build_registrar
from .registrars.pool import RegistrarPool
from .storage import Storage

logger = logging.getLogger(__name__)


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
        already = [
            item.domain
            for item in self.storage.list_domains(enabled_only=True)
            if item.state is DomainState.AVAILABLE
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
