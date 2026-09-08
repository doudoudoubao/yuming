"""外部命令适配器——没内置的注册商用这个接。

调用你自己的脚本，用**参数数组**传递（不经过 shell，没有命令注入面）：

    registrar:
      provider: exec
      options:
        command: ["/opt/scripts/buy.sh"]
        check_command: ["/opt/scripts/check.sh"]   # 可选
        timeout: 30

约定：
* 域名等参数以环境变量 ``DM_DOMAIN`` / ``DM_YEARS`` / ``DM_MAX_PRICE`` 传入，
  同时作为位置参数追加到命令后面。
* 退出码 0 = 成功；其它 = 失败。
* stdout 若是 JSON（``{"success":true,"order_id":"..","price":9.9}``）会被解析，
  否则整段 stdout 当作 message。
* 退出码 2 约定为「不可重试的硬错误」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from typing import Any

from ..config import PurchaseConfig
from ..models import Availability, RegistrationResult
from .base import Registrar, RegistrarError

logger = logging.getLogger(__name__)

FATAL_EXIT_CODE = 2


def _kill_process_group(process: "asyncio.subprocess.Process") -> None:
    """连同孙进程一起终止；拿不到进程组就退回只杀直接子进程。"""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("killpg 失败(%s)，退回 kill 单个进程", exc)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


class ExecRegistrar(Registrar):
    name = "exec"
    display_name = "外部脚本"
    signup_url = ""
    payment = "取决于你的脚本"
    required_options = (
        "command",
    )
    needs_contact = False
    notes = (
        "用来接任何没有内置适配器的注册商",
        "域名以第一个位置参数和 DM_DOMAIN 环境变量传入",
        "退出码 0=成功，2=不可重试的硬错误",
    )
    supports_price = False

    def _command(self, key: str) -> list[str]:
        command = self.options.get(key)
        if isinstance(command, str):
            command = [command]
        if not command or not isinstance(command, list):
            raise RegistrarError(f"exec: 缺少配置 registrar.options.{key}（需要是数组）")
        return [str(item) for item in command]

    async def _run(
        self, command: list[str], domain: str, env_extra: dict[str, str]
    ) -> tuple[int, str, str]:
        env = {**os.environ, "DM_DOMAIN": domain, **env_extra}
        timeout = float(self.options.get("timeout", self.config.timeout))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                domain,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # 单开一个进程组，超时时才能连孙进程一起收掉
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise RegistrarError(f"exec: 启动命令失败: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # 只 kill 直接子进程是不够的：它派生的孙进程会继续持有 stdout 管道，
            # 让 wait() 一直挂到孙进程自己退出，超时形同虚设。所以杀整个进程组。
            _kill_process_group(process)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2.0)
            raise RegistrarError(f"exec: 命令超时（{timeout}s）") from None

        return (
            process.returncode or 0,
            stdout.decode("utf-8", "replace").strip(),
            stderr.decode("utf-8", "replace").strip(),
        )

    async def ping(self) -> tuple[bool, str]:
        try:
            command = self._command("command")
        except RegistrarError as exc:
            return False, str(exc)
        return True, f"exec: 将调用 {' '.join(command)}"

    async def check(self, domain: str) -> Availability:
        if not self.options.get("check_command"):
            return Availability(domain=domain, available=None)
        try:
            code, stdout, stderr = await self._run(
                self._command("check_command"), domain, {"DM_ACTION": "check"}
            )
        except RegistrarError as exc:
            return Availability(domain=domain, available=None, error=str(exc))

        payload = _try_json(stdout)
        if payload is not None:
            price = payload.get("price")
            return Availability(
                domain=domain,
                available=payload.get("available"),
                price=float(price) if price is not None else None,
                currency=str(payload.get("currency", "USD")),
                raw=payload,
            )
        return Availability(
            domain=domain, available=code == 0, error=stderr or None
        )

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        try:
            command = self._command("command")
        except RegistrarError as exc:
            return self.failure(domain, str(exc), retryable=False)

        env_extra = {
            "DM_ACTION": "register",
            "DM_YEARS": str(self.years_for(purchase, years)),
            "DM_MAX_PRICE": str(purchase.max_price),
            "DM_PRIVACY": "1" if purchase.whois_privacy else "0",
            "DM_AUTO_RENEW": "1" if purchase.auto_renew else "0",
            "DM_NAMESERVERS": ",".join(purchase.nameservers),
        }
        try:
            code, stdout, stderr = await self._run(command, domain, env_extra)
        except RegistrarError as exc:
            return self.failure(domain, str(exc))

        payload = _try_json(stdout) or {}
        message = str(payload.get("message") or stdout or stderr or f"退出码 {code}")

        if code == 0 and payload.get("success", True):
            price = payload.get("price")
            return RegistrationResult(
                domain=domain,
                success=True,
                provider=self.name,
                order_id=str(payload.get("order_id", "")) or None,
                price=float(price) if price is not None else None,
                currency=str(payload.get("currency", "USD")),
                message=message,
                raw=payload or None,
            )
        return self.failure(
            domain,
            message,
            raw=payload or None,
            retryable=code != FATAL_EXIT_CODE and not payload.get("fatal"),
        )


def _try_json(text: str) -> dict[str, Any] | None:
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
