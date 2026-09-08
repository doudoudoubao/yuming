import httpx
import pytest

from domain_monitor import cli
from domain_monitor.dnsprobe import DnsProbe, ProbeResult
from domain_monitor.config import DnsConfig
from tests.conftest import BOOTSTRAP, rdap_payload


@pytest.fixture
def offline(monkeypatch, rdap_server):
    """把所有出网的 AsyncClient 换成 MockTransport。"""
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(rdap_server.handler))
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return rdap_server


def write_config(tmp_path, extra=""):
    path = tmp_path / "config.yaml"
    path.write_text(
        "state_dir: '%s'\n"
        "database: 'test.db'\n"
        "rdap:\n  rps_per_host: 1000\n"
        "dns:\n  enabled: false\n"
        "telegram:\n  enabled: false\n" % tmp_path + extra,
        encoding="utf-8",
    )
    return str(path)


def test_parser_defaults_to_run():
    args = cli.build_parser().parse_args([])
    assert args.command is None      # main() 会把 None 当作 run


def test_parser_subcommands():
    parser = cli.build_parser()
    assert parser.parse_args(["check", "a.com", "b.com"]).domains == ["a.com", "b.com"]
    assert parser.parse_args(["log", "-n", "5"]).limit == 5
    assert parser.parse_args(["-c", "x.yaml", "list"]).config == "x.yaml"


def test_bad_config_exits_with_code_2(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("poll:\n  unknown_key: 1\n", encoding="utf-8")
    assert cli.main(["-c", str(path), "list"]) == 2
    assert "配置错误" in capsys.readouterr().err


def test_missing_config_file_exits_with_code_2(tmp_path, capsys):
    assert cli.main(["-c", str(tmp_path / "nope.yaml"), "list"]) == 2
    assert "配置文件不存在" in capsys.readouterr().err


def test_init_writes_template(tmp_path, capsys):
    target = tmp_path / "config.yaml"
    assert cli.main(["init", str(target)]) == 0
    assert target.exists()
    assert "domain-monitor" in capsys.readouterr().out or target.read_text(encoding="utf-8")
    # 不覆盖已有文件
    assert cli.main(["init", str(target)]) == 1


def test_add_list_remove_roundtrip(tmp_path, offline, capsys):
    config = write_config(tmp_path)

    assert cli.main(["-c", config, "add", "Example.COM", "bad domain"]) == 0
    out = capsys.readouterr().out
    assert "example.com" in out
    assert "非法域名" in out

    assert cli.main(["-c", config, "list"]) == 0
    assert "example.com" in capsys.readouterr().out

    assert cli.main(["-c", config, "rm", "example.com"]) == 0
    assert "已移出监控" in capsys.readouterr().out

    assert cli.main(["-c", config, "list"]) == 0
    assert "监控列表还是空的" in capsys.readouterr().out


def test_check_command(tmp_path, offline, capsys):
    offline.set("target.com", rdap_payload(statuses=["pending delete"]))
    config = write_config(tmp_path)

    assert cli.main(["-c", config, "check", "target.com"]) == 0
    out = capsys.readouterr().out
    assert "待删除" in out
    assert "pending delete" in out


def test_check_available_domain(tmp_path, offline, capsys):
    offline.set("free.com", None)
    config = write_config(tmp_path)
    assert cli.main(["-c", config, "check", "free.com"]) == 0
    assert "可注册" in capsys.readouterr().out


def test_check_rejects_invalid_domain(tmp_path, offline, capsys):
    config = write_config(tmp_path)
    assert cli.main(["-c", config, "check", "not a domain"]) == 2
    assert "不是合法域名" in capsys.readouterr().out


def test_once_runs_a_pass(tmp_path, offline, capsys):
    offline.set("target.com", rdap_payload())
    config = write_config(tmp_path, "domains:\n  - target.com\n")

    assert cli.main(["-c", config, "once"]) == 0
    assert "target.com" in capsys.readouterr().out


def test_price_command(tmp_path, offline, capsys):
    config = write_config(
        tmp_path,
        "registrar:\n  provider: dryrun\n  options:\n    price: 12.34\n",
    )
    assert cli.main(["-c", config, "price", "a.com"]) == 0
    assert "12.34" in capsys.readouterr().out


def test_test_command_reports(tmp_path, offline, capsys):
    offline.set("example.com", rdap_payload())
    config = write_config(tmp_path)
    cli.main(["-c", config, "test"])
    out = capsys.readouterr().out
    assert "RDAP" in out
    assert "注册商" in out
    assert "自检" in out


def test_log_command(tmp_path, offline, capsys):
    config = write_config(tmp_path)
    cli.main(["-c", config, "add", "a.com"])
    capsys.readouterr()
    assert cli.main(["-c", config, "log"]) == 0
    assert "a.com" in capsys.readouterr().out


def test_config_discovery_via_env(tmp_path, monkeypatch, offline, capsys):
    config = write_config(tmp_path)
    monkeypatch.setenv("DOMAIN_MONITOR_CONFIG", config)
    assert cli.main(["list"]) == 0


# ------------------------------------------------------------------ DNS 探测

async def test_dns_probe_disabled_returns_unknown():
    probe = DnsProbe(DnsConfig(enabled=False))
    assert probe.usable is False
    assert await probe.probe("example.com") is ProbeResult.UNKNOWN


async def test_dns_probe_rejects_bare_tld():
    probe = DnsProbe(DnsConfig(enabled=True))
    assert await probe.probe("com") is ProbeResult.UNKNOWN


async def test_dns_probe_handles_missing_nameservers(monkeypatch):
    probe = DnsProbe(DnsConfig(enabled=True))

    async def no_servers(tld):
        return []

    monkeypatch.setattr(probe, "_tld_nameservers", no_servers)
    assert await probe.probe("example.com") is ProbeResult.UNKNOWN


def test_argparse_help_is_chinese(capsys):
    """argparse 自带的 usage: / options: 是英文，必须换掉。"""
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out

    assert "用法：" in out and "选项" in out and "子命令" in out
    for leak in ("usage:", "positional arguments:", "options:",
                 "show this help message"):
        assert leak not in out, f"帮助里还留着英文：{leak}"


def test_list_shows_chinese_phase_labels(tmp_path, offline, capsys):
    """档位不能显示成 idle / sprint 这种内部标识。"""
    from domain_monitor.models import Phase
    from domain_monitor.storage import Storage

    config = write_config(tmp_path)
    cli.main(["-c", config, "add", "a.com"])
    capsys.readouterr()

    with Storage(tmp_path / "test.db") as store:
        store.update_domain("a.com", phase=Phase.SPRINT)

    cli.main(["-c", config, "list"])
    out = capsys.readouterr().out

    assert "冲刺" in out
    assert "sprint" not in out


def test_registrar_command_lists_all(tmp_path, offline, capsys):
    """用户问「在哪里下单」时，这条命令要能一次说清。"""
    config = write_config(tmp_path)

    assert cli.main(["-c", config, "registrar"]) == 0
    out = capsys.readouterr().out

    assert "本程序自己不卖域名" in out
    for provider in ("namesilo", "dynadot", "godaddy", "namecheap", "aliyun"):
        assert provider in out
    assert "当前配置的是：dryrun" in out


def test_registrar_detail_is_copy_pasteable(tmp_path, offline, capsys):
    config = write_config(tmp_path)

    assert cli.main(["-c", config, "registrar", "namecheap"]) == 0
    out = capsys.readouterr().out

    assert "provider: namecheap" in out
    for option in ("api_user", "api_key", "client_ip"):
        assert option in out
    assert "contact:" in out                    # 这家需要联系人资料
    assert "client_ip 填错是最常见的失败原因" in out
    assert "不要写进 config.yaml" in out         # 密钥去向


def test_registrar_detail_without_contact(tmp_path, offline, capsys):
    config = write_config(tmp_path)

    cli.main(["-c", config, "registrar", "namesilo"])
    out = capsys.readouterr().out

    assert "api_key" in out
    assert "contact:" not in out                # NameSilo 用账户默认资料


def test_registrar_unknown_name(tmp_path, offline, capsys):
    config = write_config(tmp_path)
    assert cli.main(["-c", config, "registrar", "nope"]) == 2
    assert "没有 nope" in capsys.readouterr().err


def test_test_command_points_at_registrar_setup(tmp_path, offline, capsys):
    """自检发现还在用演练适配器时，要告诉用户下一步去哪。"""
    config = write_config(tmp_path)
    cli.main(["-c", config, "test"])
    out = capsys.readouterr().out
    assert "domain-monitor registrar" in out
