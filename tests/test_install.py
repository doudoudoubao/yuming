"""安装相关：.env 自动加载、保留注释的配置编辑、install.sh 静态检查。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from domain_monitor.cli import build_config, build_parser, load_env_files
from domain_monitor.config import load_config
from domain_monitor.config_edit import set_domains, set_flag
from domain_monitor.utils import load_dotenv

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ .env 加载

def test_load_dotenv_basic(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# 注释\n\nFOO=bar\nexport BAZ=qux\nQUOTED='has spaces'\nDQ=\"double\"\n没有等号\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FOO", raising=False)
    for key in ("BAZ", "QUOTED", "DQ"):
        monkeypatch.delenv(key, raising=False)

    assert load_dotenv(env) == 4
    assert os.environ["FOO"] == "bar"
    assert os.environ["BAZ"] == "qux"          # export 前缀要能处理
    assert os.environ["QUOTED"] == "has spaces"
    assert os.environ["DQ"] == "double"


def test_real_env_wins_over_dotenv(tmp_path, monkeypatch):
    """真正的环境变量必须压过 .env 文件里的值。"""
    env = tmp_path / ".env"
    env.write_text("TOKEN=from_file\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN", "from_environment")

    load_dotenv(env)

    assert os.environ["TOKEN"] == "from_environment"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == 0


def test_config_reads_env_without_manual_source(tmp_path, monkeypatch):
    """装完不 source .env 也要能跑起来——否则报错信息极难看懂。"""
    config = tmp_path / "config.yaml"
    config.write_text(
        "telegram:\n  enabled: true\n  bot_token: '${TG_BOT_TOKEN}'\n"
        "  chat_id: '${TG_CHAT_ID}'\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TG_BOT_TOKEN=t0k\nTG_CHAT_ID=42\n", encoding="utf-8")
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)

    args = build_parser().parse_args(["-c", str(config), "list"])
    result = build_config(args)

    assert result.telegram.bot_token == "t0k"
    assert result.telegram.chat_id == "42"


def test_load_env_files_handles_missing_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    load_env_files(None)      # 不该抛异常


# -------------------------------------------------------- 保留注释的配置编辑

def test_set_domains_preserves_comments():
    text = load_template()
    before_comments = text.count("#")

    result = set_domains(text, ["a.com", "b.io"])

    assert yaml.safe_load(result)["domains"] == ["a.com", "b.io"]
    # 注释不能被 yaml 回写抹掉——那些注释就是这份模板的文档价值
    assert result.count("#") >= before_comments - 5
    assert "idle_interval" in result
    assert "RDAP 有速率限制" in result


def test_set_domains_empty_stays_valid_yaml():
    result = set_domains(load_template(), [])
    assert yaml.safe_load(result)["domains"] == []


def test_set_domains_clears_template_examples():
    """新装用户不该莫名其妙在监控 example.com。"""
    result = set_domains(load_template(), ["mine.com"])
    assert yaml.safe_load(result)["domains"] == ["mine.com"]


def test_set_flag_preserves_trailing_comment():
    result = set_flag(load_template(), "telegram", "enabled", "true")

    assert yaml.safe_load(result)["telegram"]["enabled"] is True
    line = next(l for l in result.splitlines()
                if l.strip().startswith("enabled: true") and "bot_token" in l)
    assert "# ← 填好下面的" in line     # 行尾注释原样保留


def test_set_flag_only_touches_its_own_section():
    """telegram.enabled 改动不能波及 dns.enabled。"""
    text = load_template()
    assert yaml.safe_load(text)["dns"]["enabled"] is True

    result = set_flag(text, "telegram", "enabled", "true")

    assert yaml.safe_load(result)["dns"]["enabled"] is True
    assert yaml.safe_load(result)["telegram"]["enabled"] is True


def test_set_flag_unknown_section_is_noop():
    text = load_template()
    assert set_flag(text, "nosuchsection", "enabled", "true") == text


def test_edited_template_still_loads():
    text = set_flag(set_domains(load_template(), ["x.com"]), "telegram", "enabled", "false")
    Path("/tmp/edited-config.yaml").write_text(text, encoding="utf-8")
    config = load_config("/tmp/edited-config.yaml")
    assert [item.name for item in config.domains] == ["x.com"]


def load_template() -> str:
    return (REPO / "config.example.yaml").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 安装脚本

def test_install_script_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(REPO / "install.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_install_script_is_executable():
    assert os.access(REPO / "install.sh", os.X_OK)


def test_install_script_rejects_unknown_flag():
    result = subprocess.run(
        ["bash", str(REPO / "install.sh"), "--nonsense"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "未知参数" in result.stderr


def test_install_script_help_works():
    result = subprocess.run(
        ["bash", str(REPO / "install.sh"), "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "install.sh" in result.stdout


def test_version_guard_message_is_actionable():
    """Python 太老时要给一句人话，而不是一堆 SyntaxError。"""
    source = (REPO / "domain_monitor" / "__init__.py").read_text(encoding="utf-8")
    assert "3, 10" in source
    assert "apt install" in source


def test_install_script_rejects_incomplete_checkout(tmp_path):
    """只把 install.sh 拷出来跑、或者 clone 错分支，要给出能照做的提示。"""
    shutil.copy(REPO / "install.sh", tmp_path / "install.sh")
    result = subprocess.run(
        ["bash", str(tmp_path / "install.sh"), "--yes"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "不是完整的项目" in combined
    assert "git clone -b" in combined          # 给出可直接照做的命令


def test_docs_clone_commands_specify_the_branch():
    """main 分支只有一个 README，文档里的 clone 命令必须带 -b。"""
    for name in ("README.md", "docs/安装.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "git clone" in line and "yuming.git" in line:
                assert "-b " in line, f"{name} 的 clone 命令没带分支: {line}"
            elif "git clone" in line:
                assert "-b " in line, f"{name} 的 clone 命令没带分支: {line}"


def test_set_domains_does_not_eat_the_next_section():
    """顶格注释是分段标志，改写 domains 段不能把下一段的文档一起删掉。"""
    text = load_template()
    assert "prefixes:" in text          # 模板里有这段（注释形式）

    result = set_domains(text, ["a.com"])

    assert "prefixes:" in result
    assert "stop_after_first" in result
    assert "pattern_limit" in yaml.safe_load(result)


def test_block_edits_are_composable():
    """连续做多次定点编辑，注释不该被一点点啃掉。"""
    text = load_template()
    result = set_domains(text, ["a.com"])
    result = set_flag(result, "telegram", "enabled", "true")
    result = set_domains(result, ["a.com", "b.com"])

    data = yaml.safe_load(result)
    assert data["domains"] == ["a.com", "b.com"]
    assert data["telegram"]["enabled"] is True
    assert result.count("#") >= text.count("#") - 6
