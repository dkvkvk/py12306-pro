"""配置与密钥管理测试（规格书 3.1 / 3.2 / 5.4 / 7 / 9）。

重点：字段写错必须启动即报错，而不是静默失效。
"""

from __future__ import annotations

import json
import logging

import pytest

from railkit.config import (
    Config,
    ConfigError,
    HARD_MIN_QUERY_INTERVAL,
    RECOMMENDED_MIN_QUERY_INTERVAL,
    Secret,
    build_config,
    in_period,
    load_config,
    load_dotenv,
    load_legacy_env,
    parse_periods,
    to_minutes,
)


def env_of(base_env, **overrides):
    env = dict(base_env)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def problems_of(base_env, **overrides):
    try:
        build_config(env_of(base_env, **overrides), strict=True)
    except ConfigError as exc:
        return exc.problems
    return []


class TestHappyPath:
    def test_minimal_config_loads(self, base_env, tmp_path):
        config = build_config(base_env, base_dir=tmp_path)
        assert isinstance(config, Config)
        assert len(config.accounts) == 1
        assert config.accounts[0].username == "tester"
        assert config.accounts[0].password.reveal() == "s3cret-pwd"
        assert config.query.interval_seconds == 4.0
        assert config.web.jwt_secret.reveal() == "x" * 48

    def test_paths_are_relative_to_base_dir(self, base_env, tmp_path):
        env = env_of(base_env, RUNTIME_DIR="runtime", DATA_DIR="data", LOG_DIR="logs")
        config = build_config(env, base_dir=tmp_path)
        assert config.paths.state_dir == tmp_path / "runtime"
        assert config.paths.data_dir == tmp_path / "data"

    def test_multiple_accounts_from_json(self, base_env, tmp_path):
        accounts = [
            {"username": "a", "password": "p1", "login_type": "qr"},
            {"username": "b", "password": "p2", "login_type": "sso"},
        ]
        config = build_config(env_of(base_env, USER_ACCOUNTS_JSON=json.dumps(accounts)), base_dir=tmp_path)
        assert [a.username for a in config.accounts] == ["a", "b"]

    def test_single_account_object_accepted(self, base_env, tmp_path):
        payload = json.dumps({"username": "solo", "login_type": "qr"})
        config = build_config(env_of(base_env, USER_ACCOUNTS_JSON=payload), base_dir=tmp_path)
        assert config.accounts[0].username == "solo"

    def test_password_from_env_registers_as_secret(self, base_env, tmp_path):
        config = build_config(env_of(base_env, USER_ACCOUNTS_JSON=None, USERNAME="u", PASSWORD="p", LOGIN_TYPE="qr"), base_dir=tmp_path)
        assert config.accounts[0].password.reveal() == "p"


class TestAccountValidation:
    def test_missing_accounts_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, USER_ACCOUNTS_JSON=None)
        assert any("没有配置任何账号" in p for p in problems)

    def test_bad_json_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, USER_ACCOUNTS_JSON="{not json}")
        assert any("不是合法 JSON" in p for p in problems)

    def test_json_must_be_list_or_object(self, base_env, tmp_path):
        problems = problems_of(base_env, USER_ACCOUNTS_JSON='"a string"')
        assert any("必须是数组" in p for p in problems)

    def test_entry_without_username_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, USER_ACCOUNTS_JSON='[{"password":"x"}]')
        assert any("缺少 username" in p for p in problems)

    def test_non_object_entry_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, USER_ACCOUNTS_JSON='["oops"]')
        assert any("不是对象" in p for p in problems)

    def test_invalid_login_type_fails(self, base_env, tmp_path):
        """写错字段名的典型后果：静默不工作。这里必须报错。"""
        payload = json.dumps({"username": "who", "login_type": None, "south": ""})
        problems = problems_of(base_env, USER_ACCOUNTS_JSON=payload, LOGIN_TYPE="qrcode")
        assert any("LOGIN_TYPE" in p and "非法" in p for p in problems)

    def test_password_login_requires_password(self, base_env, tmp_path):
        env = env_of(base_env, USER_ACCOUNTS_JSON=None, USERNAME="u", LOGIN_TYPE="password", PASSWORD=None)
        problems = []
        try:
            build_config(env, base_dir=tmp_path, strict=True)
        except ConfigError as exc:
            problems = exc.problems
        assert any("需要提供 PASSWORD" in p for p in problems)


class TestQueryValidation:
    def test_query_interval_below_hard_min_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, QUERY_INTERVAL=str(HARD_MIN_QUERY_INTERVAL - 0.5))
        assert any("QUERY_INTERVAL" in p for p in problems)

    def test_aggressive_interval_warns_but_loads(self, base_env, tmp_path):
        config = build_config(env_of(base_env, QUERY_INTERVAL="1.5"), base_dir=tmp_path)
        assert config.query.interval_seconds == 1.5
        assert any("太激进" in w for w in config.warnings)

    def test_recommended_interval_does_not_warn(self, base_env, tmp_path):
        config = build_config(env_of(base_env, QUERY_INTERVAL=str(RECOMMENDED_MIN_QUERY_INTERVAL)), base_dir=tmp_path)
        assert not any("太激进" in w for w in config.warnings)

    def test_non_numeric_interval_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, QUERY_INTERVAL="fast")
        assert any("不是数字" in p for p in problems)

    def test_period_parsing_from_env(self, base_env, tmp_path):
        config = build_config(env_of(base_env, DEPART_PERIOD="22:00-06:00"), base_dir=tmp_path)
        assert config.query.periods == ((1320, 360),)
        assert config.query.in_dept_period("23:10")
        assert not config.query.in_dept_period("12:00")

    def test_bad_period_fails_startup(self, base_env, tmp_path):
        problems = problems_of(base_env, DEPART_PERIOD="25:00-06:00")
        assert any("非法" in p for p in problems)

    def test_max_station_pairs(self, base_env, tmp_path):
        config = build_config(env_of(base_env, MAX_STATION_PAIRS="3"), base_dir=tmp_path)
        assert config.query.max_station_pairs == 3
        warnings = build_config(env_of(base_env, MAX_STATION_PAIRS="20"), base_dir=tmp_path).warnings
        assert any("偏大" in w for w in warnings)

    def test_bool_validation(self, base_env, tmp_path):
        problems = problems_of(base_env, LOG_JSON="maybe")
        assert any("不是合法布尔值" in p for p in problems)

    def test_invalid_mode(self, base_env, tmp_path):
        problems = problems_of(base_env, CLIENT_MODE="worker")
        assert any("CLIENT_MODE" in p for p in problems)


class TestSecretsAndWeb:
    def test_missing_jwt_secret_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, JWT_SECRET_KEY=None)
        assert any("JWT_SECRET_KEY" in p for p in problems)

    def test_short_jwt_secret_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, JWT_SECRET_KEY="short")
        assert any("太短" in p for p in problems)

    def test_dev_mode_allows_missing_jwt_with_warning(self, base_env, tmp_path):
        config = build_config(env_of(base_env, JWT_SECRET_KEY=None, DEV_MODE="1"), base_dir=tmp_path)
        assert config.web.jwt_secret is None
        assert any("DEV_MODE" in w for w in config.warnings)

    def test_public_bind_requires_ip_allowlist(self, base_env, tmp_path):
        problems = problems_of(base_env, WEB_BIND="0.0.0.0", WEB_ALLOWED_IPS=None)
        assert any("裸奔公网" in p for p in problems)

    def test_public_bind_with_allowlist_is_ok(self, base_env, tmp_path):
        config = build_config(env_of(base_env, WEB_BIND="0.0.0.0", WEB_ALLOWED_IPS="10.0.0.1,10.0.0.2"), base_dir=tmp_path)
        assert config.web.allowed_ips == ("10.0.0.1", "10.0.0.2")

    def test_basic_auth_needs_password(self, base_env, tmp_path):
        problems = problems_of(base_env, WEB_BASIC_AUTH_USER="admin")
        assert any("WEB_BASIC_AUTH_PASSWORD" in p for p in problems)

    def test_missing_enc_key_warns(self, base_env, tmp_path):
        config = build_config(env_of(base_env, RUNTIME_ENC_KEY=None), base_dir=tmp_path)
        assert config.enc_key is None
        assert any("RUNTIME_ENC_KEY" in w for w in config.warnings)

    def test_short_enc_key_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, RUNTIME_ENC_KEY="tiny")
        assert any("RUNTIME_ENC_KEY" in p for p in problems)

    def test_redis_url_scheme_validated(self, base_env, tmp_path):
        problems = problems_of(base_env, REDIS_URL="127.0.0.1:6379")
        assert any("REDIS_URL" in p for p in problems)

    def test_scrub_hides_all_secrets(self, base_env, tmp_path):
        env = env_of(
            base_env,
            USER_ACCOUNTS_JSON='[{"username": "u", "password": "topsecret-pw", "login_type": "qr"}]',
        )
        config = build_config(env, base_dir=tmp_path)
        dumped = json.dumps(config.scrub(), ensure_ascii=False)
        assert "topsecret-pw" not in dumped
        assert "x" * 48 not in dumped
        assert config.accounts[0].password.reveal() == "topsecret-pw"

    def test_secret_type_hides_value_in_str_and_repr(self):
        secret = Secret("abc123")
        assert repr(secret) == "<secret len=6>"
        assert str(secret) == "<secret>"
        assert secret.reveal() == "abc123"
        assert secret == "abc123"

    def test_redaction_policy_covers_config_secrets(self, base_env, tmp_path):
        config = build_config(base_env, base_dir=tmp_path)
        masked = config.redaction_policy().redact("redis://... password s3cret-pwd redis://127.0.0.1:6379/0")
        assert "s3cret-pwd" not in masked


class TestNotifyValidation:
    def test_unknown_adapter_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, NOTIFY_ADAPTERS="telegram")
        assert any("未知适配器" in p for p in problems)

    def test_adapter_without_config_fails(self, base_env, tmp_path):
        problems = problems_of(base_env, NOTIFY_ADAPTERS="dingtalk")
        assert any("DINGTALK_WEBHOOK" in p for p in problems)

    def test_dingtalk_with_webhook_ok(self, base_env, tmp_path):
        config = build_config(env_of(base_env, NOTIFY_ADAPTERS="console,dingtalk", DINGTALK_WEBHOOK="https://x/y"), base_dir=tmp_path)
        assert "dingtalk" in config.notify.adapters

    def test_alias_env_names(self, base_env, tmp_path):
        config = build_config(env_of(base_env, NOTIFY_ADAPTERS="serverchan", SERVERCHAN_SENDKEY="SCT123"), base_dir=tmp_path)
        assert config.notify.serverchan_key.reveal() == "SCT123"


class TestDotenvAndLegacy:
    def test_load_dotenv(self, tmp_path):
        (tmp_path / ".env").write_text(
            "QUERY_INTERVAL=5\n# comment\nexport REDIS_URL='redis://cache:6379/1'\nWEB_BIND=127.0.0.1 # inline\n",
            encoding="utf-8",
        )
        env: dict = {}
        assert load_dotenv(tmp_path / ".env", env) is True
        assert env["QUERY_INTERVAL"] == "5"
        assert env["REDIS_URL"] == "redis://cache:6379/1"
        assert env["WEB_BIND"] == "127.0.0.1"

    def test_real_env_wins_over_dotenv(self, tmp_path):
        (tmp_path / ".env").write_text("QUERY_INTERVAL=5\n", encoding="utf-8")
        env = {"QUERY_INTERVAL": "9"}
        load_dotenv(tmp_path / ".env", env)
        assert env["QUERY_INTERVAL"] == "9"

    def test_missing_dotenv_is_not_an_error(self, tmp_path):
        assert load_dotenv(tmp_path / ".env", {}) is False

    def test_config_reads_dotenv_from_base_dir(self, tmp_path, base_env):
        (tmp_path / ".env").write_text("QUERY_INTERVAL=7\n", encoding="utf-8")
        # 真实环境变量优先于 .env，所以这里先清掉 base_env 里的 QUERY_INTERVAL
        config = build_config(env_of(base_env, QUERY_INTERVAL=None), base_dir=tmp_path)
        assert config.query.interval_seconds == 7.0

    def test_legacy_env_py_migrates_secrets(self, tmp_path, caplog):
        (tmp_path / "env.py").write_text(
            "USERNAME = 'legacy-user'\nPASSWORD = 'legacy-pass'\nLOGIN_TYPE = 'qr'\nREDIS_URL = 'redis://l:6379/2'\n",
            encoding="utf-8",
        )
        env: dict = {}
        with caplog.at_level(logging.WARNING):
            assert load_legacy_env(tmp_path / "env.py", env) is True
        assert env["USERNAME"] == "legacy-user"
        assert env["PASSWORD"] == "legacy-pass"
        assert env["REDIS_URL"] == "redis://l:6379/2"
        assert any("明文密钥" in rec.message for rec in caplog.records)

    def test_legacy_env_is_off_by_default(self, tmp_path, base_env):
        (tmp_path / "env.py").write_text("USERNAME = 'legacy-user'\n", encoding="utf-8")
        # 同时清掉环境变量里的账号，确保唯一的账号来源就是 env.py
        with pytest.raises(ConfigError):
            build_config(env_of(base_env, USER_ACCOUNTS_JSON=None), base_dir=tmp_path, strict=True)


class TestStrictFlag:
    def test_strict_false_collects_without_raising(self, base_env, tmp_path):
        config = build_config(env_of(base_env, JWT_SECRET_KEY=None), base_dir=tmp_path, strict=False)
        assert config.web.jwt_secret is None

    def test_config_error_message_lists_all_problems(self, base_env, tmp_path):
        with pytest.raises(ConfigError) as exc:
            build_config(env_of(base_env, JWT_SECRET_KEY=None, QUERY_INTERVAL="0.1"), base_dir=tmp_path)
        assert len(exc.value.problems) >= 2
        assert "配置校验失败" in str(exc.value)

    def test_load_config_is_strict(self, base_env, tmp_path):
        with pytest.raises(ConfigError):
            load_config(env_of(base_env, JWT_SECRET_KEY=None), base_dir=tmp_path)


class TestTimeHelpers:
    def test_to_minutes_boundaries(self):
        assert to_minutes("00:00") == 0
        assert to_minutes("12:34") == 754

    def test_parse_periods_multiple(self):
        assert parse_periods("22:00-06:00, 08:00-10:00", []) == ((1320, 360), (480, 600))

    def test_in_period_cross_midnight(self):
        assert in_period("02:00", "22:00", "06:00")
        assert not in_period("12:00", "22:00", "06:00")
