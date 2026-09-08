"""注册商适配器注册表。"""

from __future__ import annotations

import httpx

from ..config import RegistrarConfig
from .aliyun import AliyunRegistrar
from .base import Registrar, RegistrarError, env_var_name
from .dryrun import DryRunRegistrar
from .dynadot import DynadotRegistrar
from .exec_provider import ExecRegistrar
from .godaddy import GoDaddyRegistrar
from .namecheap import NamecheapRegistrar
from .namesilo import NameSiloRegistrar

PROVIDERS: dict[str, type[Registrar]] = {
    "dryrun": DryRunRegistrar,
    "namesilo": NameSiloRegistrar,
    "dynadot": DynadotRegistrar,
    "godaddy": GoDaddyRegistrar,
    "namecheap": NamecheapRegistrar,
    "aliyun": AliyunRegistrar,
    "exec": ExecRegistrar,
}


def available_providers() -> list[str]:
    return sorted(PROVIDERS)


def build_registrar(
    config: RegistrarConfig, *, client: httpx.AsyncClient | None = None
) -> Registrar:
    """按配置构造注册商适配器。"""
    provider = (config.provider or "dryrun").strip().lower()
    cls = PROVIDERS.get(provider)
    if cls is None:
        raise RegistrarError(
            f"未知的注册商 provider={provider!r}，可选: {', '.join(available_providers())}"
        )
    return cls(config, client=client)


def credential_env_vars() -> dict[str, list[str]]:
    """每个注册商需要哪些环境变量。供配置模板和安装脚本使用。"""
    return {
        name: [env_var_name(name, option) for option in cls.required_options]
        for name, cls in sorted(PROVIDERS.items())
        if cls.required_options and name not in ("exec", "dryrun")
    }


__all__ = [
    "Registrar",
    "env_var_name",
    "credential_env_vars",
    "RegistrarError",
    "PROVIDERS",
    "available_providers",
    "build_registrar",
]
