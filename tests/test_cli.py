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
    assert "监控列表为空" in capsys.readouterr().out


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
    assert "自检结果" in out


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
