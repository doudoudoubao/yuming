import pytest

from domain_monitor.config import ConfigError, load_config


def test_defaults_are_safe():
    config = load_config(data={})
    # 默认必须是「不会花钱」的状态
    assert config.purchase.enabled is False
    assert config.purchase.dry_run is True
    assert config.registrar.provider == "dryrun"


def test_domain_shorthand_and_full_form():
    config = load_config(
        data={"domains": ["a.com", {"name": "B.NET", "max_price": 20, "note": "x"}]}
    )
    assert [item.name for item in config.domains] == ["a.com", "b.net"]
    assert config.domains[1].max_price == 20.0
    assert config.domains[1].note == "x"


def test_duplicate_domains_collapse():
    config = load_config(data={"domains": ["a.com", "A.com", "a.com"]})
    assert len(config.domains) == 1


def test_idn_domains_are_punycoded():
    config = load_config(data={"domains": ["中文.com"]})
    assert config.domains[0].name == "xn--fiq228c.com"


def test_invalid_domain_rejected():
    with pytest.raises(ConfigError, match="不是合法域名"):
        load_config(data={"domains": ["not a domain"]})


def test_unknown_key_rejected():
    with pytest.raises(ConfigError, match="未知配置项"):
        load_config(data={"poll": {"typo_here": 1}})


def test_unknown_top_level_key_rejected():
    with pytest.raises(ConfigError, match="顶层存在未知配置项"):
        load_config(data={"pol": {}})


def test_int_coerced_to_float():
    config = load_config(data={"poll": {"idle_interval": 60}})
    assert isinstance(config.poll.idle_interval, float)


def test_negative_interval_rejected():
    with pytest.raises(ConfigError, match="不能为负数"):
        load_config(data={"poll": {"idle_interval": -1}})


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("TOKEN", "t0k3n")
    monkeypatch.setenv("CHAT", "42")
    config = load_config(
        data={"telegram": {"enabled": True, "bot_token": "${TOKEN}", "chat_id": "${CHAT}"}}
    )
    assert config.telegram.bot_token == "t0k3n"
    assert config.telegram.chat_id == "42"


def test_telegram_requires_token(monkeypatch):
    monkeypatch.delenv("TOKEN", raising=False)
    with pytest.raises(ConfigError, match="bot_token"):
        load_config(data={"telegram": {"enabled": True, "chat_id": "1"}})


def test_telegram_requires_a_recipient():
    with pytest.raises(ConfigError, match="chat_id 或 allowed_user_ids"):
        load_config(data={"telegram": {"enabled": True, "bot_token": "x"}})


def test_real_purchase_requires_real_registrar():
    with pytest.raises(ConfigError, match="必须配置真实的注册商"):
        load_config(data={"purchase": {"enabled": True, "dry_run": False}})


def test_real_purchase_requires_price_cap():
    with pytest.raises(ConfigError, match="max_price"):
        load_config(
            data={
                "purchase": {"enabled": True, "dry_run": False, "max_price": 0},
                "registrar": {"provider": "namesilo", "options": {"api_key": "k"}},
            }
        )


def test_real_purchase_requires_daily_budget():
    with pytest.raises(ConfigError, match="daily_budget"):
        load_config(
            data={
                "purchase": {"enabled": True, "dry_run": False, "daily_budget": 0},
                "registrar": {"provider": "namesilo", "options": {"api_key": "k"}},
            }
        )


def test_lifecycle_builtin_and_override():
    config = load_config(
        data={"lifecycle": {"tlds": {"com": {"pending_delete_days": 7}}}}
    )
    com = config.lifecycle.profile_for("com")
    assert com.pending_delete_days == 7
    # 未覆盖的字段仍保留内置默认
    assert com.drop_window_start == "17:30"
    assert config.lifecycle.profile_for("unknown-tld").pending_delete_days == 5


def test_lifecycle_bad_window_rejected():
    with pytest.raises(ConfigError, match="HH:MM"):
        load_config(data={"lifecycle": {"tlds": {"com": {"drop_window_start": "25h"}}}})

    with pytest.raises(ConfigError, match="不是合法时间"):
        load_config(data={"lifecycle": {"tlds": {"com": {"drop_window_start": "99:00"}}}})


def test_example_config_is_valid():
    config = load_config("config.example.yaml")
    assert config.domains
    # 模板必须默认安全：不下单
    assert config.purchase.enabled is False
    assert config.purchase.dry_run is True


def test_state_dir_resolution():
    config = load_config(data={"state_dir": "/tmp/dm", "database": "x.db"})
    assert config.database_path == "/tmp/dm/x.db"
    config2 = load_config(data={"state_dir": "/tmp/dm", "database": "/abs/y.db"})
    assert config2.database_path == "/abs/y.db"


# --------------------------------------------------------------- 多注册商通道

def test_registrars_list_parsed():
    config = load_config(
        data={
            "registrars": [
                {"provider": "namesilo", "options": {"api_key": "k"}},
                {"provider": "dynadot", "options": {"api_key": "d"}},
            ]
        }
    )
    assert [item.provider for item in config.registrar_configs] == ["namesilo", "dynadot"]


def test_single_registrar_still_works():
    """老配置文件（只有单数 registrar:）不用改也能跑。"""
    config = load_config(data={"registrar": {"provider": "aliyun"}})
    assert [item.provider for item in config.registrar_configs] == ["aliyun"]


def test_registrars_must_be_a_list():
    with pytest.raises(ConfigError, match="registrars 必须是列表"):
        load_config(data={"registrars": {"provider": "namesilo"}})


def test_registrars_unknown_key_rejected():
    with pytest.raises(ConfigError, match=r"registrars\[0\] 存在未知配置项"):
        load_config(data={"registrars": [{"provider": "namesilo", "typo": 1}]})


def test_dryrun_mixed_into_real_purchase_rejected():
    """真实下单时列表里混进假适配器要被拦住。"""
    with pytest.raises(ConfigError, match="dryrun 假适配器"):
        load_config(
            data={
                "purchase": {"enabled": True, "dry_run": False},
                "registrars": [
                    {"provider": "namesilo", "options": {"api_key": "k"}},
                    {"provider": "dryrun"},
                ],
            }
        )


def test_multi_real_registrars_accepted():
    config = load_config(
        data={
            "purchase": {"enabled": True, "dry_run": False},
            "registrars": [
                {"provider": "namesilo", "options": {"api_key": "k"}},
                {"provider": "dynadot", "options": {"api_key": "d"}},
            ],
        }
    )
    assert len(config.registrar_configs) == 2


# ------------------------------------------------ 真实下单前的凭据完整性

def test_real_purchase_requires_credentials():
    """开了真实下单却没填 API Key，必须启动就拦。

    不拦的话，域名释放那一刻才会发现，还会对着同一个错误空转上百次——
    抢注窗口早就过去了。
    """
    with pytest.raises(ConfigError, match="必填项是空的"):
        load_config(
            data={
                "purchase": {"enabled": True, "dry_run": False},
                "registrar": {"provider": "namesilo", "options": {"api_key": ""}},
            }
        )


def test_credential_error_names_the_env_var():
    """报错要能直接照做，不能只说「缺配置」。"""
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            data={
                "purchase": {"enabled": True, "dry_run": False},
                "registrar": {"provider": "namesilo"},
            }
        )
    message = str(excinfo.value)
    assert "NAMESILO_API_KEY" in message
    assert "domain-monitor registrar namesilo" in message
    assert "dry_run" in message                  # 告诉用户怎么退回安全状态


def test_real_purchase_requires_contact_when_needed():
    """GoDaddy / Namecheap 下单要提交注册人资料，空着一样下不了单。"""
    with pytest.raises(ConfigError, match="注册人资料"):
        load_config(
            data={
                "purchase": {"enabled": True, "dry_run": False},
                "registrar": {
                    "provider": "godaddy",
                    "options": {"api_key": "k", "api_secret": "s"},
                },
            }
        )


def test_dry_run_does_not_require_credentials():
    """演练模式不该被凭据校验挡住——它的用途就是没凭据也能跑通链路。"""
    config = load_config(
        data={
            "purchase": {"enabled": True, "dry_run": True},
            "registrar": {"provider": "namesilo", "options": {"api_key": ""}},
        }
    )
    assert config.purchase.dry_run is True


def test_monitor_only_does_not_require_credentials():
    load_config(
        data={
            "purchase": {"enabled": False},
            "registrar": {"provider": "namesilo"},
        }
    )


def test_multi_registrar_credentials_all_checked():
    """多通道时任何一家缺凭据都要报出来，并指明是第几个。"""
    with pytest.raises(ConfigError, match=r"registrars\[1\]"):
        load_config(
            data={
                "purchase": {"enabled": True, "dry_run": False},
                "registrars": [
                    {"provider": "namesilo", "options": {"api_key": "k"}},
                    {"provider": "dynadot", "options": {"api_key": ""}},
                ],
            }
        )
