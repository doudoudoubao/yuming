"""对 config.yaml 做保留注释的定点编辑。

安装脚本要往配置里写域名、打开 Telegram，但模板里那一大堆注释就是文档，
用 yaml.safe_dump 回写会把它们全部抹掉。所以这里按行做定点修改。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _block_range(lines: list[str], key: str) -> tuple[int, int]:
    """找到顶层 ``key:`` 段的行区间 [起始行, 结束行)，找不到返回 (-1, -1)。"""
    start = -1
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            start = index
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        # 顶格的任何内容都算下一段的开始——包括顶格注释。
        # 顶格注释就是分段标志（"# ---- 前缀监控"），把它算进本段的话，
        # 改写这一段会顺手删掉下一段的整段文档。
        if not line[0].isspace():
            end = index
            break
    return start, end


def set_domains(text: str, names: list[str]) -> str:
    """替换 domains: 段的条目，保留该段前后的注释。"""
    lines = text.splitlines()
    start, end = _block_range(lines, "domains")
    entries = [f"  - {name}" for name in names] if names else []
    if not entries:
        entries = ["  []  # 还没有监控任何域名；用 /add 或 domain_monitor add 添加"]

    if start < 0:
        return text.rstrip("\n") + "\n\ndomains:\n" + "\n".join(entries) + "\n"

    # 段内开头的注释行保留下来（它们是对这个段的说明）
    kept: list[str] = []
    for line in lines[start + 1 : end]:
        if line.strip().startswith("#"):
            kept.append(line)
        elif line.strip():
            break
    rebuilt = [lines[start], *kept, *entries]
    return "\n".join([*lines[:start], *rebuilt, *lines[end:]]) + "\n"


def set_flag(text: str, section: str, key: str, value: str) -> str:
    """把 ``section`` 段里 ``key`` 的值改成 value，行尾注释保留。"""
    lines = text.splitlines()
    start, end = _block_range(lines, section)
    if start < 0:
        return text
    for index in range(start + 1, end):
        stripped = lines[index].lstrip()
        if not stripped.startswith(f"{key}:"):
            continue
        indent = lines[index][: len(lines[index]) - len(stripped)]
        # 行尾注释原样保留（含 # 后面的空格），只换掉值
        _, sep, tail = lines[index].partition("#")
        comment = f"  #{tail}" if sep else ""
        lines[index] = f"{indent}{key}: {value}{comment}"
        break
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: config_edit.py <config.yaml> domains [域名...] | telegram-on", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    action = sys.argv[2]
    text = path.read_text(encoding="utf-8")

    if action == "domains":
        text = set_domains(text, sys.argv[3:])
    elif action == "telegram-on":
        text = set_flag(text, "telegram", "enabled", "true")
    else:
        print(f"未知操作: {action}", file=sys.stderr)
        return 2

    path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
