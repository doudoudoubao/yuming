"""监控引擎：调度、状态机、抢注。

核心思路是**按剩余时间分档提速**：

    平时(idle)      6 小时查一次 RDAP
    进入删除流程    30 分钟一次
    临近预测释放    1 分钟一次
    冲刺(sprint)    DNS 高频探测 + 并发下单

RDAP 有速率限制，所以只在低频档用它；冲刺阶段改用便宜的 DNS 探测做触发，
真正判定「能不能注册」交给注册商 API——它才是最终裁判，而且顺手就把单下了。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from .config import AppConfig
from .dnsprobe import DnsProbe, ProbeResult
from .models import (
    DomainState,
    DomainStatus,
    Phase,
    RegistrationResult,
    WatchedDomain,
    estimate_drop_time,
)
from .notify.telegram import Notifier, NullBot
from .rdap import RdapClient
from .registrars.base import Registrar
from .registrars.pool import RegistrarPool
from .storage import Storage
from .tldgroups import RESTRICTED_NOTES, group_names
from .utils import (
    PatternError,
    apply_jitter,
    escape_html,
    expand_patterns,
    human_delta,
    human_until,
    is_valid_domain,
    next_window_occurrence,
    normalize_domain,
    tld_of,
    to_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

PAUSED_KEY = "purchase_paused"


class Engine:
    """把存储、查询、注册、通知串起来的主控。"""

    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        rdap: RdapClient,
        registrar: Registrar | RegistrarPool,
        notifier: Notifier,
        *,
        probe: DnsProbe | None = None,
        bot: Any | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.rdap = rdap
        # 统一走通道池：单个注册商就是只有一个成员的池子
        self.pool = registrar if isinstance(registrar, RegistrarPool) else RegistrarPool([registrar])
        self.notifier = notifier
        self.probe = probe or DnsProbe(config.dns)
        self.bot = bot or NullBot()
        self._stop = asyncio.Event()
        self._sprints: dict[str, asyncio.Task[None]] = {}
        self._acquiring: set[str] = set()
        self._semaphore = asyncio.Semaphore(config.poll.concurrency)
        self.started_at = utcnow()

    @property
    def registrar(self) -> Registrar:
        """主注册商通道。

        赋值会**重建整个通道池**——否则改了 registrar 却仍走旧池子，
        是个很难发现的静默失效。
        """
        return self.pool.primary

    @registrar.setter
    def registrar(self, value: Registrar | RegistrarPool) -> None:
        self.pool = value if isinstance(value, RegistrarPool) else RegistrarPool([value])

    # ------------------------------------------------------------------ 暂停开关

    @property
    def paused(self) -> bool:
        return bool(self.storage.get_kv(PAUSED_KEY, False))

    def set_paused(self, value: bool) -> None:
        self.storage.set_kv(PAUSED_KEY, bool(value))

    def stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------------- 主循环

    async def run(self) -> None:
        """常驻主循环。"""
        logger.info(
            "引擎启动：%d 个域名，注册商=%s，下单=%s",
            len(self.storage.list_domains()),
            self.registrar.name,
            "开启" if self.config.purchase.enabled else "关闭（仅监控）",
        )
        if self.config.purchase.enabled and self.config.purchase.dry_run:
            logger.warning("purchase.dry_run=true —— 只演练不会真的下单")

        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - 主循环必须活着
                logger.exception("巡检出错")
                await self.notifier.error("巡检出错，详见日志")

            delay = self._sleep_seconds()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue
        await self._cancel_sprints()

    def _sleep_seconds(self) -> float:
        """睡到下一个域名该查的时刻，上限 60 秒以便及时响应停止信号。"""
        upcoming = self.storage.earliest_next_check()
        if upcoming is None:
            return 5.0
        remaining = (to_utc(upcoming) - utcnow()).total_seconds()
        return max(0.5, min(60.0, remaining))

    async def run_once(self) -> list[DomainStatus]:
        """跑一轮巡检：查所有到点的域名。"""
        due = self.storage.due_domains(limit=500)
        if not due:
            return []

        logger.debug("本轮待查 %d 个域名", len(due))
        results = await asyncio.gather(
            *(self._guarded_check(item) for item in due), return_exceptions=True
        )
        statuses: list[DomainStatus] = []
        for item, result in zip(due, results):
            if isinstance(result, BaseException):
                logger.error("检查 %s 失败: %s", item.domain, result)
                continue
            statuses.append(result)
        return statuses

    async def _guarded_check(self, watched: WatchedDomain) -> DomainStatus:
        async with self._semaphore:
            return await self.check_domain(watched)

    # -------------------------------------------------------------------- 单域名

    async def check_domain(self, watched: WatchedDomain) -> DomainStatus:
        """查询一个域名并推进它的状态机。"""
        status = await self.rdap.lookup(watched.domain)
        await self._apply_status(watched, status)
        return status

    async def _apply_status(self, watched: WatchedDomain, status: DomainStatus) -> None:
        now = utcnow()
        previous = watched.state

        if status.state == DomainState.ERROR:
            # 查询失败绝不当成「可注册」，只记录错误并延后重试
            self.storage.update_domain(
                watched.domain,
                last_checked_at=now,
                last_error=status.error,
                next_check_at=now + timedelta(
                    seconds=max(self.config.poll.error_backoff, 1.0)
                ),
            )
            logger.warning("%s 查询失败: %s", watched.domain, status.error)
            return

        if status.state == DomainState.AVAILABLE and not await self._trust_available(
            watched, previous
        ):
            # 复核没通过：当成一次抖动，不改状态、不推送，稍后重查
            self.storage.update_domain(
                watched.domain,
                last_checked_at=now,
                next_check_at=now + timedelta(
                    seconds=max(self.config.poll.near_interval, 1.0)
                ),
            )
            # 把返回值也修正成实际采信的状态，免得 CLI / 调用方回显那个被否掉的读数
            status.state = previous
            status.error = "疑似误报，已忽略本次读数"
            return

        # pendingDelete / 赎回期的起点：优先用 RDAP 的 last changed，
        # 否则用「我们第一次观测到该状态」的时间兜底
        # 起点取值顺序：RDAP 的 last changed（权威）→ 我们首次观测到该状态的时间 → 现在。
        # 必须持久化首次观测时间，否则每轮都用 now 会让预测释放时间无限往后滑。
        pending_since = None
        if status.state == DomainState.PENDING_DELETE:
            pending_since = status.changed_at or watched.pending_delete_since or now

        redemption_since = None
        if status.state == DomainState.REDEMPTION:
            redemption_since = status.changed_at or watched.redemption_since or now

        profile = self.config.lifecycle.profile_for(tld_of(watched.domain))
        drop_at = estimate_drop_time(
            status.state,
            profile,
            expires_at=status.expires_at,
            pending_delete_since=pending_since,
            redemption_since=redemption_since,
        )
        drop_at = self._snap_to_drop_window(drop_at, profile)

        phase = self._phase_for(status.state, drop_at, now)
        interval = self._interval_for(phase)
        next_check = now + timedelta(seconds=apply_jitter(interval, self.config.poll.jitter))

        self.storage.update_domain(
            watched.domain,
            state=status.state,
            statuses=status.statuses,
            registrar=status.registrar,
            expires_at=status.expires_at,
            drop_at=drop_at,
            pending_delete_since=pending_since,
            redemption_since=redemption_since,
            last_checked_at=now,
            next_check_at=next_check,
            phase=phase,
            last_error=None,
        )

        if status.state != previous:
            detail = self._transition_detail(status, drop_at)
            logger.info(
                "%s 状态变化 %s -> %s %s",
                watched.domain, previous.value, status.state.value, detail,
            )
            self.storage.add_event(
                "state_change",
                domain=watched.domain,
                message=f"{previous.label} → {status.state.label} {detail}".strip(),
                data=status.to_dict(),
            )
            await self.notifier.state_changed(
                watched.domain, previous, status.state, detail=detail
            )

        if status.state == DomainState.AVAILABLE:
            await self._on_available(watched.domain, reason="RDAP 查询显示可注册")
        else:
            self._sync_sprint(watched.domain, phase, drop_at)

    async def _trust_available(
        self, watched: WatchedDomain, previous: DomainState
    ) -> bool:
        """判断这次「可注册」是真的，还是一次查询抖动。

        预期内的掉落（走完删除流程、或我们已经预测到它该掉了）直接采信，
        一秒都不耽误。只有「一个看起来还健康的域名突然 404」才复核一次——
        这种更像是 RDAP 服务器临时抽风。

        复核只能识别**间歇性**的假 404：复核时服务器明确说「还注册着」才算证伪。
        如果复核请求本身也失败，什么都证明不了，那就采信第一次读数继续走。

        注意：后缀路由错误（兜底入口不认识某个后缀而直接回 404）不归这里管，
        它在 RDAP 层就被拦成「查询失败」了，根本不会走到这个函数。
        """
        if not self.config.rdap.reverify_available:
            return True
        # UNKNOWN=首次检查（域名本来就可能没被注册过）；其余是删除流程里的正常出口
        if previous in (
            DomainState.UNKNOWN,
            DomainState.AVAILABLE,
            DomainState.EXPIRED,
            DomainState.REDEMPTION,
            DomainState.PENDING_DELETE,
        ):
            return True

        # 有些注册局根本不在 RDAP 里公布 redemption / pendingDelete，域名会从
        # 「已注册」直接消失。只要我们**预测到**它该掉了，就同样按预期处理，
        # 否则真正该抢的那一刻反而要多等一个复核往返。
        if watched.phase in (Phase.NEAR, Phase.SPRINT):
            return True
        now = utcnow()
        if watched.drop_at is not None and (
            to_utc(watched.drop_at) - now
        ).total_seconds() <= self.config.poll.watch_lead:
            return True
        if watched.expires_at is not None and to_utc(watched.expires_at) < now:
            return True

        delay = max(0.0, self.config.rdap.reverify_delay)
        logger.warning(
            "%s 从「%s」直接跳到「可注册」，可疑，%.0fs 后复核一次",
            watched.domain, previous.label, delay,
        )
        if delay:
            await asyncio.sleep(delay)

        second = await self.rdap.lookup(watched.domain)
        if second.state == DomainState.AVAILABLE:
            logger.warning("%s 复核确认可注册", watched.domain)
            return True

        if second.state == DomainState.ERROR:
            # 复核请求本身失败，什么也证明不了。这里必须采信第一次读数继续走：
            # 误报的代价是一次被注册商驳回的下单，漏掉真实掉落的代价是域名没了。
            message = (
                f"复核请求失败（{second.error}），无法证伪，"
                f"按第一次读数继续处理"
            )
            logger.warning("%s %s", watched.domain, message)
            self.storage.add_event(
                "reverify_inconclusive", domain=watched.domain,
                message=message, level="warning",
            )
            return True

        message = (
            f"疑似误报：状态从「{previous.label}」跳到「可注册」，"
            f"复核结果是「{second.state.label}」，已忽略本次"
        )
        logger.warning("%s %s", watched.domain, message)
        self.storage.add_event(
            "false_positive", domain=watched.domain, message=message, level="warning"
        )
        return False

    def _transition_detail(self, status: DomainStatus, drop_at: datetime | None) -> str:
        parts: list[str] = []
        if status.expires_at:
            parts.append(f"到期 {to_utc(status.expires_at):%Y-%m-%d}")
        if drop_at:
            parts.append(f"预计释放 {to_utc(drop_at):%Y-%m-%d %H:%M} UTC（{human_until(drop_at)}）")
        return "，".join(parts)

    # -------------------------------------------------------------------- 节奏

    def _snap_to_drop_window(
        self, drop_at: datetime | None, profile: Any
    ) -> datetime | None:
        """把预测时间对齐到该 TLD 的经验删除窗口起点。

        注册局并不承诺固定时刻，窗口只是用来决定「几点开始高频探测」。
        """
        if drop_at is None or not profile.drop_window_start:
            return drop_at
        start, _ = next_window_occurrence(
            drop_at, profile.drop_window_start, profile.drop_window_end or profile.drop_window_start
        )
        return start

    def _phase_for(
        self, state: DomainState, drop_at: datetime | None, now: datetime
    ) -> Phase:
        if state == DomainState.ACQUIRED:
            return Phase.IDLE
        if drop_at is None:
            return Phase.WATCH if state.is_droppable else Phase.IDLE

        remaining = (to_utc(drop_at) - now).total_seconds()
        poll = self.config.poll
        if remaining > poll.watch_lead:
            return Phase.IDLE
        if remaining > poll.near_lead:
            return Phase.WATCH
        if remaining > poll.sprint_lead:
            return Phase.NEAR
        if remaining > -poll.sprint_tail:
            return Phase.SPRINT
        # 预测时间过了还没掉，说明预测偏了，退回加密监控继续等
        return Phase.WATCH

    def _interval_for(self, phase: Phase) -> float:
        poll = self.config.poll
        return {
            Phase.IDLE: poll.idle_interval,
            Phase.WATCH: poll.watch_interval,
            Phase.NEAR: poll.near_interval,
            Phase.SPRINT: poll.near_interval,  # 冲刺时 RDAP 仍然低频，靠 DNS 顶上
        }[phase]

    # -------------------------------------------------------------------- 冲刺

    def _sync_sprint(self, domain: str, phase: Phase, drop_at: datetime | None) -> None:
        """根据档位启动或收掉冲刺任务。"""
        task = self._sprints.get(domain)
        if phase == Phase.SPRINT:
            if task is None or task.done():
                logger.warning(
                    "%s 进入冲刺阶段（预计释放 %s）",
                    domain,
                    f"{to_utc(drop_at):%Y-%m-%d %H:%M} UTC" if drop_at else "未知",
                )
                self.storage.add_event(
                    "sprint_start", domain=domain, message="进入冲刺阶段", level="warning"
                )
                self._sprints[domain] = asyncio.create_task(
                    self._sprint(domain, drop_at), name=f"sprint:{domain}"
                )
        elif task is not None and not task.done():
            task.cancel()
            self._sprints.pop(domain, None)

    async def _sprint(self, domain: str, drop_at: datetime | None) -> None:
        """冲刺循环：高频 DNS 探测，一旦看到 NXDOMAIN 就下单。"""
        poll = self.config.poll
        deadline = (to_utc(drop_at) if drop_at else utcnow()) + timedelta(
            seconds=poll.sprint_tail
        )
        probes = 0
        try:
            while utcnow() < deadline and not self._stop.is_set():
                if not self.probe.usable:
                    # 没有 dnspython 就退化成按 sprint_interval 直接问注册商
                    await self._on_available(domain, reason="冲刺阶段直接尝试下单",
                                             confirmed=False, fast=True)
                    await asyncio.sleep(max(poll.sprint_interval, 1.0))
                    continue

                result = await self.probe.probe(domain)
                probes += 1
                if result == ProbeResult.NXDOMAIN:
                    logger.warning("%s DNS 探测到 NXDOMAIN（第 %d 次探测），触发抢注",
                                   domain, probes)
                    self.storage.add_event(
                        "drop_detected",
                        domain=domain,
                        message=f"DNS 探测 NXDOMAIN，累计探测 {probes} 次",
                        level="warning",
                    )
                    # DNS 信号有假阳性（注册着但没设 NS 的域名也返回 NXDOMAIN）。
                    # 默认直接下单抢时间——注册商是最终裁判，白跑一次代价极低；
                    # 想稳一点就开 skip_rdap_confirm=false，多花一次 RDAP 往返做复核。
                    if not self.config.purchase.skip_rdap_confirm:
                        status = await self.rdap.lookup(domain)
                        if status.state != DomainState.AVAILABLE:
                            logger.info(
                                "%s RDAP 复核未确认可注册（%s），判定为假信号，继续探测",
                                domain, status.state.value,
                            )
                            await asyncio.sleep(max(self.config.dns.interval, 0.05))
                            continue

                    acquired = await self._on_available(
                        domain, reason="DNS 探测到域名已从注册局消失",
                        confirmed=False, fast=True,
                    )
                    if acquired:
                        return
                    # 没抢到就继续盯——可能是「注册着但没设 NS」的假信号
                await asyncio.sleep(max(self.config.dns.interval, 0.05))
        except asyncio.CancelledError:
            logger.info("%s 冲刺任务被取消", domain)
            raise
        finally:
            self._sprints.pop(domain, None)
            logger.info("%s 冲刺结束，共探测 %d 次", domain, probes)

    async def _cancel_sprints(self) -> None:
        tasks = [task for task in self._sprints.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sprints.clear()

    # -------------------------------------------------------------------- 抢注

    async def _on_available(
        self, domain: str, *, reason: str, confirmed: bool = True, fast: bool = False
    ) -> bool:
        """域名看起来可注册了，走一遍闸门再下单。返回是否抢到。"""
        if domain in self._acquiring:
            return False

        watched = self.storage.get_domain(domain)
        if watched is None or not watched.enabled:
            return False
        if watched.state == DomainState.ACQUIRED or self.storage.has_successful_purchase(domain):
            return False

        if confirmed:
            self.storage.add_event("available", domain=domain, message=reason, level="warning")
            await self.notifier.available(domain, reason)

        if not self.config.purchase.enabled:
            logger.warning("%s 可注册，但 purchase.enabled=false，仅通知不下单", domain)
            return False
        if self.paused:
            logger.warning("%s 可注册，但抢注已被 /pause 暂停", domain)
            await self.notifier.send(
                f"⏸ <b>{escape_html(domain)}</b> 可注册，但抢注处于暂停状态（/resume 恢复）"
            )
            return False

        self._acquiring.add(domain)
        try:
            result = await self._acquire(watched, reason=reason, fast=fast)
        finally:
            self._acquiring.discard(domain)
        return bool(result and result.success)

    async def _acquire(
        self, watched: WatchedDomain, *, reason: str, fast: bool = False
    ) -> RegistrationResult | None:
        """执行下单：查价 → 校验预算 → （可选）TG 确认 → 并发重试下单。

        ``fast=True`` 用于冲刺：跳过比价，省下几百毫秒直接开抢。
        抢注是毫秒级竞争，几美元的价差远不如抢到本身值钱。
        """
        domain = watched.domain
        purchase = self.config.purchase
        price_limit = watched.max_price if watched.max_price is not None else purchase.max_price
        years = watched.years or purchase.years
        price: float | None = None
        currency = purchase.currency

        self.pool.reset()
        if fast:
            logger.info("%s 冲刺下单，跳过比价直接开抢", domain)
        elif purchase.check_price_first:
            resolved = await self._resolve_price(domain, price_limit)
            if resolved is None:      # 报价超上限，放弃
                return None
            price, currency = resolved

        # 预算闸门：价格未知时按上限保守估算
        estimated = price if price is not None else price_limit
        spent = self.storage.spend_today()
        if not purchase.dry_run and spent + estimated > purchase.daily_budget:
            message = (
                f"今日已花费 {spent:.2f}，再买需 {estimated:.2f}，"
                f"超过每日预算 {purchase.daily_budget:.2f}，放弃下单"
            )
            logger.error("%s %s", domain, message)
            self.storage.add_event("budget_reject", domain=domain, message=message, level="error")
            await self.notifier.failed(domain, message)
            return None

        if purchase.confirm_via_telegram:
            approved = await self.bot.confirm_purchase(
                domain, price, currency, purchase.confirm_timeout
            )
            if not approved:
                self.storage.add_event(
                    "confirm_rejected", domain=domain, message="用户未确认下单", level="warning"
                )
                return None

        return await self._attempt_loop(domain, years, price_limit, reason)

    async def _resolve_price(
        self, domain: str, price_limit: float
    ) -> tuple[float | None, str] | None:
        """查价。配了多个通道就比价并把最便宜的一家提到前面。

        返回 ``(价格, 币种)``；返回 ``None`` 表示报价超过上限、应当放弃下单。
        元组里价格为 ``None`` 表示查不到价——查不到不等于买不了，继续下单。
        """
        purchase = self.config.purchase
        multi = len(self.pool.active) > 1 and purchase.compare_prices

        if not multi:
            registrar = self.pool.primary
            if not registrar.supports_price:
                return None, purchase.currency
            availability = await registrar.check(domain)
            if availability.error:
                logger.warning("%s 查价失败: %s（继续尝试下单）", domain, availability.error)
            if availability.premium:
                logger.warning("%s 是溢价域名（premium），价格可能远高于常规", domain)
            if availability.price is None:
                return None, availability.currency or purchase.currency
            if availability.price > price_limit:
                await self._reject_price(domain, availability.price, availability.currency,
                                         price_limit)
                return None
            return availability.price, availability.currency

        quotes = await self.pool.compare(domain)
        priced = [(item, quote) for item, quote in quotes if quote.price is not None]
        summary = "、".join(
            f"{item.label} {quote.price:.2f} {quote.currency}"
            if quote.price is not None else f"{item.label} 未知"
            for item, quote in quotes
        )
        logger.info("%s 多通道比价：%s", domain, summary)
        self.storage.add_event(
            "price_compare", domain=domain, message=summary,
            data={item.label: quote.price for item, quote in quotes},
        )

        if not priced:
            logger.warning("%s 所有通道都查不到价，直接按原顺序下单", domain)
            return None, purchase.currency

        affordable = [
            (item, quote) for item, quote in priced
            if quote.price <= price_limit and quote.available is not False
        ]
        if not affordable:
            best = priced[0][1]
            await self._reject_price(domain, best.price, best.currency, price_limit)
            return None

        winner, quote = affordable[0]
        # 把最便宜的一家排到最前，让 plan_shots 优先打它
        self.pool.prioritize(winner.label)
        logger.warning(
            "%s 选定最便宜通道 %s（%.2f %s），共比较 %d 家",
            domain, winner.label, quote.price, quote.currency, len(quotes),
        )
        return quote.price, quote.currency

    async def _reject_price(
        self, domain: str, price: float, currency: str, limit: float
    ) -> None:
        message = f"最低报价 {price:.2f} {currency} 超过上限 {limit:.2f}，放弃下单"
        logger.warning("%s %s", domain, message)
        self.storage.add_event("price_reject", domain=domain, message=message, level="warning")
        await self.notifier.failed(domain, message)

    async def _attempt_loop(
        self, domain: str, years: int, price_limit: float, reason: str
    ) -> RegistrationResult | None:
        """并发 + 重试地下单，直到成功 / 用尽次数 / 遇到硬错误。"""
        purchase = self.config.purchase
        deadline = utcnow() + timedelta(seconds=purchase.attempt_window)
        attempts = 0
        last: RegistrationResult | None = None
        saw_timeout = False

        logger.warning(
            "开始抢注 %s（%s）：最多 %d 次，%d 路并发，通道 %s，窗口 %s",
            domain, reason, purchase.max_attempts, purchase.attempt_concurrency,
            "/".join(item.label for item in self.pool.active),
            human_delta(purchase.attempt_window),
        )

        while attempts < purchase.max_attempts and utcnow() < deadline:
            if self._stop.is_set():
                break
            if not self.pool.active:
                return await self._abort_all_channels_down(domain, attempts)

            remaining = purchase.max_attempts - attempts
            winner, results = await self._register_round(domain, years, remaining)
            attempts += max(1, len(results))
            self.storage.bump_attempts(domain, max(1, len(results)))

            if winner is not None:
                return await self._on_acquired(winner, attempts)

            for item in results:
                last = item
                if "超时" in item.message or "timeout" in item.message.lower():
                    saw_timeout = True

            # 单通道时一次硬错误就该停；多通道时只停用出错那家，其余继续
            if not self.pool.active:
                return await self._abort_all_channels_down(domain, attempts)

            await asyncio.sleep(purchase.attempt_interval)

        message = f"尝试 {attempts} 次未成功"
        if last is not None:
            message += f"，最后一次：{last.message}"
        if saw_timeout:
            message += "\n⚠️ 期间有请求超时，请到注册商后台确认是否已经产生订单"
        logger.warning("%s %s", domain, message)
        self.storage.add_event("acquire_failed", domain=domain, message=message, level="warning")
        await self.notifier.failed(domain, message)
        return last

    async def _register_round(
        self, domain: str, years: int, remaining: int
    ) -> tuple[RegistrationResult | None, list[RegistrationResult]]:
        """打一轮下单。演练模式直接返回假成功，不碰任何注册商。"""
        purchase = self.config.purchase
        if purchase.dry_run:
            logger.info("[dry-run] 假装为 %s 下单 %d 年", domain, years)
            fake = RegistrationResult(
                domain=domain,
                success=True,
                provider=f"{self.pool.primary.label}(dry-run)",
                order_id="dry-run",
                price=0.0,
                message="dry_run=true，未产生真实订单",
            )
            return fake, [fake]

        return await self.pool.race_register(
            domain,
            purchase,
            years=years,
            concurrency=purchase.attempt_concurrency,
            limit=remaining,
            parallel=purchase.parallel_registrars,
        )

    async def _abort_all_channels_down(
        self, domain: str, attempts: int
    ) -> RegistrationResult | None:
        """所有注册商通道都因硬错误停用，没法再抢了。"""
        reasons = "；".join(
            f"{label}: {reason}" for label, reason in self.pool.disabled_reasons.items()
        )
        message = f"全部 {len(self.pool)} 个注册商通道均已停用，停止抢注（尝试 {attempts} 次）\n{reasons}"
        logger.error("%s %s", domain, message)
        self.storage.add_event("acquire_abort", domain=domain, message=message, level="error")
        await self.notifier.failed(domain, message)
        return None

    async def _on_acquired(self, result: RegistrationResult, attempts: int) -> RegistrationResult:
        """抢到了：落库、通知、按需摘除监控。"""
        purchase = self.config.purchase
        self.storage.record_purchase(result, dry_run=purchase.dry_run)
        self.storage.update_domain(
            result.domain,
            state=DomainState.ACQUIRED,
            acquired_at=utcnow(),
            phase=Phase.IDLE,
            enabled=not purchase.stop_after_success,
            next_check_at=None,
        )
        task = self._sprints.pop(result.domain, None)
        if task is not None and not task.done():
            task.cancel()

        detail_parts = [f"注册商={result.provider}", f"尝试 {attempts} 次"]
        if result.order_id:
            detail_parts.append(f"订单={result.order_id}")
        if result.price is not None:
            detail_parts.append(f"金额={result.price:.2f} {result.currency}")
        if purchase.dry_run:
            detail_parts.append("（演练模式，未真实下单）")
        detail = "，".join(detail_parts)

        logger.warning("🎉 抢注成功 %s：%s", result.domain, detail)
        self.storage.add_event(
            "acquired", domain=result.domain, message=detail, level="warning",
            data=result.to_dict(),
        )
        await self.notifier.acquired(result.domain, detail)
        await self._retire_group_siblings(result.domain)
        return result

    async def _retire_group_siblings(self, domain: str) -> None:
        """同一个前缀展开出来的一组域名，抢到任意一个之后把其余的撤下来。

        场景是「这个名字我要，哪个后缀都行」——已经拿到手了就没必要
        继续盯着 .net .io 白烧配额。
        """
        watched = self.storage.get_domain(domain)
        if watched is None or not watched.stop_after_first or not watched.group:
            return

        siblings = self.storage.group_siblings(domain)
        if not siblings:
            return

        for item in siblings:
            self.storage.set_enabled(item.domain, False)
            task = self._sprints.pop(item.domain, None)
            if task is not None and not task.done():
                task.cancel()

        names = "、".join(item.domain for item in siblings)
        message = f"已抢到同组的 {domain}，停止监控同组其余 {len(siblings)} 个：{names}"
        logger.warning(message)
        self.storage.add_event(
            "group_retired", domain=domain, message=message, level="warning"
        )
        await self.notifier.send(
            f"🧹 已拿到 <b>{escape_html(domain)}</b>，"
            f"同组另外 {len(siblings)} 个已停止监控：\n"
            f"<code>{escape_html(names)}</code>"
        )

    # ------------------------------------------------- Telegram 命令（Controller）

    async def cmd_list(self) -> str:
        domains = self.storage.list_domains()
        if not domains:
            return "监控列表为空，用 /add &lt;域名&gt; 添加"

        lines = [f"<b>监控列表（{len(domains)}）</b>"]
        for item in domains[:40]:
            row = f"{item.state.emoji} <code>{escape_html(item.domain)}</code> {item.state.label}"
            if not item.enabled:
                row += "（已停用）"
            if item.drop_at:
                row += f"\n    预计释放 {to_utc(item.drop_at):%m-%d %H:%M}Z {human_until(item.drop_at)}"
            elif item.expires_at:
                row += f"\n    到期 {to_utc(item.expires_at):%Y-%m-%d}"
            lines.append(row)
        if len(domains) > 40:
            lines.append(f"…… 另有 {len(domains) - 40} 个未显示")
        return "\n".join(lines)

    async def cmd_status(self) -> str:
        stats = self.storage.stats()
        purchase = self.config.purchase
        uptime = human_delta((utcnow() - self.started_at).total_seconds())
        by_state = "、".join(
            f"{DomainState(key).label} {value}"
            for key, value in sorted(stats["by_state"].items())
        ) or "无"

        mode = "关闭（仅监控）"
        if purchase.enabled:
            mode = "演练（dry-run）" if purchase.dry_run else "真实下单"
        if self.paused:
            mode += " ⏸ 已暂停"

        return "\n".join([
            "<b>系统状态</b>",
            f"运行时长：{uptime}",
            f"监控域名：{stats['total']}（启用 {stats['enabled']}）",
            f"状态分布：{by_state}",
            f"注册商通道：<code>{escape_html('、'.join(self.pool.labels))}</code>",
            f"抢注模式：{mode}",
            f"下单尝试：{stats['purchase_attempts']} 次，成功 {stats['purchase_wins']} 次",
            f"今日花费：{stats['spend_today']:.2f} / {purchase.daily_budget:.2f}",
            f"冲刺中：{len(self._sprints)} 个",
            f"DNS 探测：{'可用' if self.probe.usable else '不可用（未装 dnspython 或已禁用）'}",
        ])

    async def cmd_check(self, domain: str) -> str:
        name = normalize_domain(domain)
        if not is_valid_domain(name):
            return f"❌ 不是合法域名：{escape_html(domain)}"

        status = await self.rdap.lookup(name)
        if status.state == DomainState.ERROR:
            return f"⚠️ <code>{escape_html(name)}</code> 查询失败：{escape_html(status.error or '')}"

        lines = [f"{status.state.emoji} <code>{escape_html(name)}</code> <b>{status.state.label}</b>"]
        if status.registrar:
            lines.append(f"注册商：{escape_html(status.registrar)}")
        if status.expires_at:
            lines.append(
                f"到期：{to_utc(status.expires_at):%Y-%m-%d} {human_until(status.expires_at)}"
            )
        if status.statuses:
            lines.append(f"EPP 状态：{escape_html(', '.join(status.statuses))}")
        if status.nameservers:
            lines.append(f"NS：{escape_html(', '.join(status.nameservers[:4]))}")

        profile = self.config.lifecycle.profile_for(tld_of(name))
        drop_at = self._snap_to_drop_window(
            estimate_drop_time(
                status.state, profile,
                expires_at=status.expires_at,
                pending_delete_since=status.changed_at if status.state == DomainState.PENDING_DELETE else None,
                redemption_since=status.changed_at if status.state == DomainState.REDEMPTION else None,
            ),
            profile,
        )
        if drop_at:
            lines.append(
                f"预计释放：{to_utc(drop_at):%Y-%m-%d %H:%M} UTC（{human_until(drop_at)}）"
            )
        if self.storage.get_domain(name) is None:
            lines.append("\n未在监控列表中，用 /add 加入")
        return "\n".join(lines)

    async def cmd_info(self, domain: str) -> str:
        name = normalize_domain(domain)
        watched = self.storage.get_domain(name)
        if watched is None:
            return f"<code>{escape_html(name)}</code> 不在监控列表中"

        lines = [
            f"{watched.state.emoji} <code>{escape_html(name)}</code> <b>{watched.state.label}</b>",
            f"档位：{watched.phase.value}",
            f"启用：{'是' if watched.enabled else '否'}",
            f"来源：{watched.source}",
            f"下单尝试：{watched.attempts} 次",
        ]
        if watched.registrar:
            lines.append(f"注册商：{escape_html(watched.registrar)}")
        if watched.expires_at:
            lines.append(f"到期：{to_utc(watched.expires_at):%Y-%m-%d}")
        if watched.drop_at:
            lines.append(
                f"预计释放：{to_utc(watched.drop_at):%Y-%m-%d %H:%M} UTC "
                f"（{human_until(watched.drop_at)}）"
            )
        if watched.last_checked_at:
            lines.append(f"上次检查：{to_utc(watched.last_checked_at):%m-%d %H:%M}Z")
        if watched.next_check_at:
            lines.append(f"下次检查：{human_until(watched.next_check_at)}")
        if watched.max_price is not None:
            lines.append(f"价格上限：{watched.max_price:.2f}")
        if watched.note:
            lines.append(f"备注：{escape_html(watched.note)}")
        if watched.last_error:
            lines.append(f"最近错误：{escape_html(watched.last_error)}")
        return "\n".join(lines)

    async def cmd_add(self, domains: list[str]) -> str:
        # 支持 mydream.{com,net,io} 这种一次加一批的写法
        try:
            expanded = expand_patterns(
                domains, limit=self.config.pattern_limit, groups=self.config.tld_groups
            )
        except PatternError as exc:
            return f"❌ {escape_html(exc)}"

        added, skipped, invalid = [], [], []
        for raw in expanded[: self.config.pattern_limit]:
            name = normalize_domain(raw)
            if not is_valid_domain(name):
                invalid.append(raw)
                continue
            if self.storage.upsert_domain(name, source="telegram"):
                added.append(name)
                self.storage.add_event("added", domain=name, message="经 Telegram 加入监控")
            else:
                skipped.append(name)

        lines: list[str] = []
        if added:
            lines.append("✅ 已加入监控：\n" + "\n".join(
                f"<code>{escape_html(item)}</code>" for item in added
            ))
        if skipped:
            lines.append("ℹ️ 已在监控中：" + "、".join(
                f"<code>{escape_html(item)}</code>" for item in skipped
            ))
        if invalid:
            lines.append("❌ 非法域名：" + "、".join(escape_html(item) for item in invalid))
        return "\n\n".join(lines) or "没有可添加的域名"

    @property
    def pattern_groups(self) -> dict[str, list[str]]:
        """后缀合集，供 Telegram 侧做模式识别。"""
        return self.config.tld_groups

    async def cmd_tlds(self, name: str | None = None) -> str:
        """列出可用的后缀合集，或某个合集的具体内容。"""
        groups = self.config.tld_groups
        if name:
            key = name.strip().lstrip("@").lower()
            if key not in groups:
                return (
                    f"没有 <code>@{escape_html(key)}</code> 这个合集。发 /tlds 看全部。"
                )
            items = groups[key]
            note = RESTRICTED_NOTES.get(key)
            lines = [
                f"<b>@{escape_html(key)}</b>（{len(items)} 个后缀）",
                f"<code>{escape_html(' '.join(items))}</code>",
                "",
                f"用法：<code>你的前缀.{{@{escape_html(key)}}}</code>",
            ]
            if note:
                lines.append(f"\n⚠️ {escape_html(note)}")
            return "\n".join(lines)

        lines = ["<b>可用的后缀合集</b>", ""]
        for key in group_names():
            items = groups.get(key, [])
            preview = "、".join(items[:6])
            if len(items) > 6:
                preview += f" …… 共 {len(items)} 个"
            mark = " ⚠️" if key in RESTRICTED_NOTES else ""
            lines.append(f"<code>@{key}</code>{mark} — {escape_html(preview)}")
        lines += [
            "",
            "用法：<code>vps.{@two}</code> 一次盯一批",
            "也能混写：<code>vps.{@two,com,net}</code>",
            "发 <code>/tlds two</code> 看某个合集的完整内容",
        ]
        return "\n".join(lines)

    async def cmd_remove(self, domain: str) -> str:
        name = normalize_domain(domain)
        task = self._sprints.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
        if self.storage.remove_domain(name):
            self.storage.add_event("removed", domain=name, message="经 Telegram 移出监控")
            return f"🗑 已移出监控：<code>{escape_html(name)}</code>"
        return f"<code>{escape_html(name)}</code> 不在监控列表中"

    async def cmd_buy(self, domain: str) -> str:
        name = normalize_domain(domain)
        if not is_valid_domain(name):
            return f"❌ 不是合法域名：{escape_html(name)}"
        if not self.config.purchase.enabled:
            return "❌ purchase.enabled=false，未开启下单功能"
        if self.storage.get_domain(name) is None:
            self.storage.upsert_domain(name, source="telegram")

        watched = self.storage.get_domain(name)
        if watched is None:
            return "❌ 无法加入监控列表"

        asyncio.create_task(
            self._manual_buy(watched), name=f"manual-buy:{name}"
        )
        mode = "演练" if self.config.purchase.dry_run else "真实下单"
        return f"🛒 已开始尝试注册 <code>{escape_html(name)}</code>（{mode}），结果会推送给你"

    async def _manual_buy(self, watched: WatchedDomain) -> None:
        if watched.domain in self._acquiring:
            await self.notifier.send(
                f"ℹ️ <code>{escape_html(watched.domain)}</code> 已经在抢注中了"
            )
            return
        self._acquiring.add(watched.domain)
        try:
            await self._acquire(watched, reason="Telegram /buy 手动触发")
        except Exception as exc:  # noqa: BLE001
            logger.exception("手动下单 %s 失败", watched.domain)
            await self.notifier.failed(watched.domain, f"手动下单异常：{exc}")
        finally:
            self._acquiring.discard(watched.domain)

    async def cmd_pause(self, paused: bool) -> str:
        self.set_paused(paused)
        self.storage.add_event(
            "pause" if paused else "resume",
            message="经 Telegram 暂停抢注" if paused else "经 Telegram 恢复抢注",
            level="warning",
        )
        if paused:
            return "⏸ 已暂停自动抢注（仍继续监控和推送），/resume 恢复"
        return "▶️ 已恢复自动抢注"

    async def cmd_log(self, limit: int) -> str:
        events = self.storage.recent_events(limit)
        if not events:
            return "暂无事件记录"
        icons = {"error": "❌", "warning": "⚠️", "info": "·"}
        lines = [f"<b>最近 {len(events)} 条事件</b>"]
        for event in events:
            icon = icons.get(event.level, "·")
            target = f" <code>{escape_html(event.domain)}</code>" if event.domain else ""
            lines.append(
                f"{icon} {to_utc(event.created_at):%m-%d %H:%M}Z{target} "
                f"{escape_html(event.message)}"
            )
        return "\n".join(lines)
