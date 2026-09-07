"""把各个组件装配成一个可运行的应用。"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from .config import AppConfig
from .dnsprobe import DnsProbe
from .engine import Engine
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
        """启动时给 Telegram 报个到，顺便说明当前是不是真的会下单。"""
        if not self.telegram.enabled:
            return
        purchase = self.config.purchase
        if not purchase.enabled:
            mode = "仅监控（不会下单）"
        elif purchase.dry_run:
            mode = "演练 dry-run（不会真的下单）"
        else:
            mode = f"⚠️ 真实下单（单价上限 {purchase.max_price:.2f}，日预算 {purchase.daily_budget:.2f}）"
        count = len(self.storage.list_domains(enabled_only=True))
        await self.notifier.send(
            "🚀 <b>域名监控已启动</b>\n"
            f"监控域名：{count}\n"
            f"注册商通道：<code>{'、'.join(self.pool.labels)}</code>\n"
            f"模式：{mode}\n"
            "发送 /help 查看命令",
            quiet=True,
        )
