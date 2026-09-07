"""RDAP 查询客户端。

RDAP 是 WHOIS 的官方继任者：返回 JSON、走 HTTPS、有统一的
bootstrap 机制（IANA 维护 TLD → RDAP 服务器的映射），比解析各家
WHOIS 的自由文本可靠得多，也不需要机器上装 whois 命令。

约定：
* HTTP 404 => 该域名在注册局里不存在 => 可注册
* HTTP 200 => 已注册，解析 status / events 判断生命周期阶段
* 429/5xx => 退避重试，绝不把失败当成「可注册」
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .config import RdapConfig
from .models import DomainState, DomainStatus, classify_statuses
from .utils import Backoff, TokenBucket, parse_datetime, suffixes_of, utcnow

logger = logging.getLogger(__name__)

# IANA bootstrap 没收录的后缀，手动兜底（国家域名居多）
FALLBACK_SERVERS: dict[str, str] = {
    "cn": "https://rdap.cnnic.cn/",
    "hk": "https://rdap.hkirc.hk/",
    "tw": "https://rdap.twnic.tw/",
}


class RdapError(Exception):
    """RDAP 查询失败（网络错误、限流、返回体损坏等）。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class RdapClient:
    """带 bootstrap 缓存与 per-host 限速的 RDAP 客户端。"""

    def __init__(
        self,
        config: RdapConfig,
        *,
        cache_path: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.cache_path = Path(cache_path) if cache_path else None
        self._client = client
        self._owns_client = client is None
        self._services: dict[str, str] = {}
        self._services_loaded_at = 0.0
        self._bootstrap_lock = asyncio.Lock()
        self._buckets: dict[str, TokenBucket] = {}
        self._backoffs: dict[str, Backoff] = {}

    # ----------------------------------------------------------------- 生命周期

    async def __aenter__(self) -> "RdapClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "application/rdap+json, application/json;q=0.9",
                },
            )
            self._owns_client = True

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("RdapClient 未初始化，请先 await client.start()")
        return self._client

    # ------------------------------------------------------------------ bootstrap

    async def _load_bootstrap(self) -> dict[str, str]:
        """加载 IANA 的 TLD → RDAP 服务器映射，本地缓存 bootstrap_ttl 秒。"""
        if self._services and (time.time() - self._services_loaded_at) < self.config.bootstrap_ttl:
            return self._services

        async with self._bootstrap_lock:
            if self._services and (
                time.time() - self._services_loaded_at
            ) < self.config.bootstrap_ttl:
                return self._services

            payload: dict[str, Any] | None = None
            if self.cache_path and self.cache_path.exists():
                age = time.time() - self.cache_path.stat().st_mtime
                if age < self.config.bootstrap_ttl:
                    try:
                        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                        logger.debug("使用本地 RDAP bootstrap 缓存（%.0f 秒前）", age)
                    except (json.JSONDecodeError, OSError):
                        payload = None

            if payload is None:
                try:
                    response = await self.client.get(self.config.bootstrap_url)
                    response.raise_for_status()
                    payload = response.json()
                    if self.cache_path:
                        try:
                            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                            self.cache_path.write_text(
                                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                            )
                        except OSError as exc:
                            logger.warning("写入 bootstrap 缓存失败: %s", exc)
                except (httpx.HTTPError, json.JSONDecodeError) as exc:
                    # 拉不到就退回过期缓存，总比完全不能用强
                    if self.cache_path and self.cache_path.exists():
                        try:
                            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                            logger.warning("拉取 bootstrap 失败(%s)，改用过期缓存", exc)
                        except (json.JSONDecodeError, OSError):
                            payload = None
                    if payload is None:
                        logger.warning("拉取 RDAP bootstrap 失败: %s，仅使用内置兜底表", exc)
                        payload = {"services": []}

            services: dict[str, str] = {}
            for entry in payload.get("services", []):
                if len(entry) != 2:
                    continue
                tlds, urls = entry
                if not urls:
                    continue
                url = next((item for item in urls if str(item).startswith("https://")), urls[0])
                for tld in tlds:
                    services[str(tld).lower().lstrip(".")] = str(url)

            for tld, url in FALLBACK_SERVERS.items():
                services.setdefault(tld, url)

            self._services = services
            self._services_loaded_at = time.time()
            logger.info("RDAP bootstrap 已加载，覆盖 %d 个后缀", len(services))
            return self._services

    async def server_for(self, domain: str) -> str | None:
        """按最长后缀匹配找 RDAP 服务器，overrides 优先。"""
        candidates = suffixes_of(domain)
        for suffix in candidates:
            override = self.config.overrides.get(suffix)
            if override:
                return override.rstrip("/") + "/"

        services = await self._load_bootstrap()
        for suffix in candidates:
            url = services.get(suffix)
            if url:
                return url.rstrip("/") + "/"

        # 最后兜底：rdap.org 这类聚合入口会替我们做 302 跳转
        if self.config.fallback_service:
            return self.config.fallback_service.rstrip("/") + "/"
        return None

    # -------------------------------------------------------------------- 限速

    def _bucket(self, host: str) -> TokenBucket:
        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = TokenBucket(self.config.rps_per_host, self.config.burst_per_host)
            self._buckets[host] = bucket
        return bucket

    def _backoff(self, host: str) -> Backoff:
        backoff = self._backoffs.get(host)
        if backoff is None:
            backoff = Backoff()
            self._backoffs[host] = backoff
        return backoff

    # -------------------------------------------------------------------- 查询

    async def lookup(self, domain: str) -> DomainStatus:
        """查询单个域名，永远返回 DomainStatus（失败时 state=ERROR）。"""
        try:
            return await self._lookup(domain)
        except RdapError as exc:
            logger.warning("RDAP 查询 %s 失败: %s", domain, exc)
            return DomainStatus(domain=domain, state=DomainState.ERROR, error=str(exc))

    async def _lookup(self, domain: str) -> DomainStatus:
        server = await self.server_for(domain)
        if server is None:
            raise RdapError(f"找不到 {domain} 对应的 RDAP 服务器（该后缀可能不支持 RDAP）",
                            retryable=False)

        url = f"{server}domain/{domain}"
        host = httpx.URL(url).host
        backoff = self._backoff(host)
        last_error = "未知错误"

        for attempt in range(self.config.max_retries + 1):
            await backoff.wait()
            await self._bucket(host).acquire()
            try:
                response = await self.client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"网络错误: {exc}"
                backoff.penalize()
                continue

            if response.status_code == 404:
                backoff.reset()
                return DomainStatus(domain=domain, state=DomainState.AVAILABLE, source="rdap")

            if response.status_code == 200:
                backoff.reset()
                try:
                    return parse_rdap(domain, response.json())
                except (json.JSONDecodeError, ValueError) as exc:
                    raise RdapError(f"RDAP 返回体解析失败: {exc}", retryable=False) from exc

            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = _retry_after(response)
                delay = backoff.penalize(retry_after)
                last_error = f"HTTP {response.status_code}（退避 {delay:.0f}s）"
                logger.warning("RDAP %s 返回 %s，退避 %.0fs", host, response.status_code, delay)
                continue

            if response.status_code in (400, 403, 422):
                raise RdapError(f"HTTP {response.status_code}: {response.text[:200]}",
                                retryable=False)

            last_error = f"HTTP {response.status_code}"
            backoff.penalize()

        raise RdapError(f"重试 {self.config.max_retries + 1} 次仍失败：{last_error}")

    async def is_available(self, domain: str) -> bool | None:
        """只关心「能不能注册」时的快捷方法，None 表示查不出来。"""
        status = await self.lookup(domain)
        if status.state == DomainState.ERROR:
            return None
        return status.state == DomainState.AVAILABLE


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        parsed = parse_datetime(value)
        if parsed is None:
            return None
        return max(0.0, (parsed - utcnow()).total_seconds())


def _vcard_name(entity: dict[str, Any]) -> str | None:
    """从 jCard（vcardArray）里抠出 fn 字段。"""
    vcard = entity.get("vcardArray")
    if not isinstance(vcard, list) or len(vcard) < 2:
        return None
    for item in vcard[1]:
        if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
            return str(item[3])
    return None


def _registrar_of(payload: dict[str, Any]) -> str | None:
    for entity in payload.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        roles = [str(role).lower() for role in entity.get("roles") or []]
        if "registrar" in roles:
            name = _vcard_name(entity)
            if name:
                return name
            for ident in entity.get("publicIds") or []:
                if isinstance(ident, dict) and ident.get("identifier"):
                    return f"IANA#{ident['identifier']}"
            if entity.get("handle"):
                return str(entity["handle"])
    return None


def parse_rdap(domain: str, payload: dict[str, Any]) -> DomainStatus:
    """把 RDAP 响应体解析成 DomainStatus。"""
    if not isinstance(payload, dict):
        raise ValueError("RDAP 响应不是 JSON 对象")

    statuses = [str(item) for item in (payload.get("status") or [])]

    events: dict[str, Any] = {}
    for event in payload.get("events") or []:
        if isinstance(event, dict) and event.get("eventAction"):
            events.setdefault(str(event["eventAction"]).lower(), event.get("eventDate"))

    expires_at = parse_datetime(events.get("expiration"))
    registered_at = parse_datetime(events.get("registration"))
    changed_at = parse_datetime(events.get("last changed")) or parse_datetime(
        events.get("last update of rdap database")
    )

    nameservers = [
        str(item.get("ldhName"))
        for item in payload.get("nameservers") or []
        if isinstance(item, dict) and item.get("ldhName")
    ]

    state = classify_statuses(statuses, expires_at)

    return DomainStatus(
        domain=domain,
        state=state,
        statuses=statuses,
        registrar=_registrar_of(payload),
        nameservers=nameservers,
        registered_at=registered_at,
        expires_at=expires_at,
        changed_at=changed_at,
        source="rdap",
        raw=payload,
    )
