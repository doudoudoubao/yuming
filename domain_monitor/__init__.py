"""域名监控与自动抢注系统。

监控目标域名的注册状态（RDAP + DNS 探测），在域名被释放的瞬间
通过注册商 API 自动下单注册，并通过 Telegram 机器人推送 / 交互。
"""

import sys

# dataclass(slots=True) 需要 3.10。版本不够时给一句人话，
# 而不是让用户对着一堆 SyntaxError 发懵。
if sys.version_info < (3, 10):  # pragma: no cover - 只在老版本上触发
    raise RuntimeError(
        f"域名监控需要 Python 3.10 或更高版本，当前是 "
        f"{sys.version_info.major}.{sys.version_info.minor}。\n"
        f"Ubuntu/Debian:  sudo apt install python3.11 python3.11-venv\n"
        f"CentOS/RHEL:    sudo dnf install python3.11\n"
        f"macOS:          brew install python@3.11"
    )

__version__ = "1.0.0"
__all__ = ["__version__"]
