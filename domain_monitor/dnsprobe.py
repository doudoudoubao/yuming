"""DNS 快速探测。

冲刺阶段不能用 RDAP 高频轮询——会被限流甚至封 IP。替代方案是直接问
TLD 的权威服务器要目标域名的 NS 记录：

* NOERROR + 有 NS 委派 => 域名还在注册局里活着
* NXDOMAIN              => 注册局里已经没有这条记录了（域名被删除的强信号）

这是**信号不是结论**：一个注册着但没设置 NS 的域名同样返回 NXDOMAIN。
所以命中之后要么用 RDAP 复核，要么（抢时间时）直接让注册商 API 去判定——
注册商那边如果域名还没释放，下单只会返回一个错误，代价很低。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from enum import Enum

from .config import DnsConfig
from .utils import domain_labels, tld_of

logger = logging.getLogger(__name__)

try:  # dnspython 是可选依赖，没装就自动降级
    import dns.asyncquery
    import dns.asyncresolver
    import dns.message
    import dns.rcode
    import dns.rdatatype
    import dns.resolver

    DNS_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    DNS_AVAILABLE = False


class ProbeResult(str, Enum):
    DELEGATED = "delegated"    # 注册局里有委派，域名还活着
    NXDOMAIN = "nxdomain"      # 注册局里查无此域，可能刚被删
    UNKNOWN = "unknown"        # 探测失败，不作任何判断


class DnsProbe:
    """向 TLD 权威服务器直接发查询的轻量探测器。"""

    def __init__(self, config: DnsConfig) -> None:
        self.config = config
        self._ns_cache: dict[str, tuple[list[str], float]] = {}
        self._lock = asyncio.Lock()

    @property
    def usable(self) -> bool:
        return self.config.enabled and DNS_AVAILABLE

    async def _tld_nameservers(self, tld: str) -> list[str]:
        """解析并缓存某个 TLD 的权威服务器 IP。"""
        if self.config.resolvers:
            return list(self.config.resolvers)

        cached = self._ns_cache.get(tld)
        if cached and (time.time() - cached[1]) < self.config.cache_ttl:
            return cached[0]

        async with self._lock:
            cached = self._ns_cache.get(tld)
            if cached and (time.time() - cached[1]) < self.config.cache_ttl:
                return cached[0]

            addresses: list[str] = []
            try:
                resolver = dns.asyncresolver.Resolver()
                resolver.lifetime = self.config.timeout
                answer = await resolver.resolve(f"{tld}.", "NS")
                names = [str(item.target).rstrip(".") for item in answer]
                random.shuffle(names)
                for name in names[:4]:
                    try:
                        record = await resolver.resolve(f"{name}.", "A")
                        addresses.extend(str(item.address) for item in record)
                    except Exception:  # noqa: BLE001 - 单个 NS 解析失败不致命
                        continue
            except Exception as exc:  # noqa: BLE001
                logger.debug("解析 .%s 权威服务器失败: %s", tld, exc)

            if addresses:
                self._ns_cache[tld] = (addresses, time.time())
            return addresses

    async def probe(self, domain: str) -> ProbeResult:
        """探测一个域名在注册局侧是否还存在委派。"""
        if not self.usable:
            return ProbeResult.UNKNOWN
        if len(domain_labels(domain)) < 2:
            return ProbeResult.UNKNOWN

        tld = tld_of(domain)
        servers = await self._tld_nameservers(tld)
        if not servers:
            return ProbeResult.UNKNOWN

        query = dns.message.make_query(f"{domain}.", dns.rdatatype.NS)
        for address in servers[:2]:
            try:
                response = await dns.asyncquery.udp(
                    query, address, timeout=self.config.timeout
                )
            except Exception as exc:  # noqa: BLE001 - 超时/网络抖动都当 UNKNOWN
                logger.debug("DNS 探测 %s @%s 失败: %s", domain, address, exc)
                continue

            rcode = response.rcode()
            if rcode == dns.rcode.NXDOMAIN:
                return ProbeResult.NXDOMAIN
            if rcode == dns.rcode.NOERROR:
                has_records = any(
                    record.rdtype in (dns.rdatatype.NS, dns.rdatatype.SOA)
                    for section in (response.answer, response.authority)
                    for record in section
                )
                # NOERROR 但既无 NS 也无 SOA，说明这条名字在区里不存在委派
                return ProbeResult.DELEGATED if has_records else ProbeResult.NXDOMAIN
        return ProbeResult.UNKNOWN

    async def looks_deleted(self, domain: str) -> bool:
        """便捷封装：只有明确的 NXDOMAIN 才返回 True。"""
        return await self.probe(domain) == ProbeResult.NXDOMAIN
