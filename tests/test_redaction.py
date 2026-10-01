"""日志脱敏测试（规格书 3.3）：日志里不能出现完整 cookie、密码、身份证号、手机号、token。"""

from __future__ import annotations

import logging

import pytest

from railkit.redaction import (
    install,
    COOKIE,
    ID_CARD,
    PASSWORD,
    PHONE,
    SECRET,
    TOKEN,
    RedactingFilter,
    RedactionPolicy,
    install_everywhere,
    redact,
)


@pytest.fixture()
def policy():
    return RedactionPolicy()


class TestBasicRules:
    def test_mobile_number(self, policy):
        assert "13812345678" not in policy.redact("phone=13812345678")
        assert PHONE in policy.redact("phone=13812345678")

    def test_mobile_with_country_code(self, policy):
        out = policy.redact("tel: +8613812345678")
        assert "13812345678" not in out

    def test_masked_mobile_keeps_no_tail(self, policy):
        """123****5678 这种已打码形式也不能把尾号留下。"""
        out = policy.redact("手机号 138****5678 已注册")
        assert "5678" not in out.split("手机号")[1][:20] or PHONE in out

    def test_id_card_18(self, policy):
        out = policy.redact("身份证 11010119900307721X")
        assert "11010119900307721X" not in out
        assert ID_CARD in out

    def test_id_card_15(self, policy):
        out = policy.redact("id=110101900307721")
        assert "110101900307721" not in out
        assert ID_CARD in out

    def test_email(self, policy):
        assert "user.name+tag@example.co.uk" not in policy.redact("mailto user.name+tag@example.co.uk")

    def test_password_assignments(self, policy):
        for text in ['password=hunter2', '"password": "hunter2"', "passwd: hunter2", "pwd='hunter2'"]:
            out = policy.redact(text)
            assert "hunter2" not in out, text
            assert PASSWORD in out

    def test_token_assignments(self, policy):
        out = policy.redact("access_token=abcdef1234567890&x=1")
        assert "abcdef1234567890" not in out
        assert TOKEN in out

    def test_json_cookie(self, policy):
        out = policy.redact('{"cookie": "JSESSIONID=ABC123; RAIL_DEVICEID=XYZ"}')
        assert "ABC123" not in out
        assert "XYZ" not in out

    def test_cookie_header_keeps_names(self, policy):
        out = policy.redact("Set-Cookie: RAIL_DEVICEID=abcdefg123456;JSESSIONID=zzz999")
        assert "RAIL_DEVICEID" in out
        assert "abcdefg123456" not in out
        assert "zzz999" not in out

    def test_long_opaque_token(self, policy):
        value = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        out = policy.redact("AUTH=" + value)
        assert value not in out
        assert SECRET in out

    def test_plain_long_word_not_touched(self, policy):
        word = "refundableNonexchangeableTicketsToday"
        assert policy.redact(word) == word

    def test_empty_and_none_like(self, policy):
        assert policy.redact("") == ""
        assert policy.redact("no secrets here") == "no secrets here"


class TestKnownSecrets:
    def test_registered_secret_is_replaced_anywhere(self):
        policy = RedactionPolicy(literals=["s3cret-pwd-value"])
        out = policy.redact("connecting with s3cret-pwd-value -> ok")
        assert "s3cret-pwd-value" not in out

    def test_register_secret_ignores_short_values(self):
        policy = RedactionPolicy()
        policy.register_secret("abc")
        assert policy.redact("abc def") == "abc def"

    def test_longer_literal_replaced_first(self):
        policy = RedactionPolicy(literals=["abcdef", "abcdefgh"])
        assert policy.redact("abcdefgh") == SECRET

    def test_from_env_registers_env_secrets(self):
        policy = RedactionPolicy.from_env(
            {
                "JWT_SECRET_KEY": "jwt-secret-value-1234567890",
                "REDIS_URL": "redis://:redispass@127.0.0.1:6379/0",
                "LOG_REDACT_EXTRA": r"SEC-\d{6}",
            }
        )
        out = policy.redact("jwt=jwt-secret-value-1234567890 url=redis://:redispass@127.0.0.1:6379/0 code=SEC-123456")
        assert "jwt-secret-value-1234567890" not in out
        assert "redispass" not in out
        assert "SEC-123456" not in out

    def test_can_be_disabled(self):
        policy = RedactionPolicy(literals=["hunter2"], enabled=False)
        assert policy.redact("hunter2") == "hunter2"

    def test_extra_pattern_from_env(self):
        policy = RedactionPolicy.from_env({"LOG_REDACT_EXTRA": r"MYTOKEN-\w+"})
        assert "MYTOKEN-abc" not in policy.redact("x MYTOKEN-abc y")


class TestLoggingIntegration:
    def test_filter_scrubs_message_and_args(self, caplog):
        policy = RedactionPolicy(literals=["hunter2"])
        logger = logging.getLogger("railkit.test.redaction")
        logger.setLevel(logging.INFO)
        install(logger, policy)
        with caplog.at_level(logging.INFO, logger="railkit.test.redaction"):
            logger.info("login %s with password=%s", "tester", "hunter2")
        text = caplog.text
        assert "hunter2" not in text
        assert "tester" in text

    def test_filter_handles_dict_args(self, caplog):
        policy = RedactionPolicy()
        logger = logging.getLogger("railkit.test.dictargs")
        logger.setLevel(logging.INFO)
        install(logger, policy)
        with caplog.at_level(logging.INFO, logger="railkit.test.dictargs"):
            logger.info("user %(user)s phone %(phone)s", {"user": "a", "phone": "13812345678"})
        assert "13812345678" not in caplog.text

    def test_install_everywhere_is_idempotent(self):
        policy = RedactionPolicy()
        install_everywhere(policy)
        install_everywhere(policy)
        root = logging.getLogger()
        filters = [f for f in root.filters if isinstance(f, RedactingFilter)]
        assert len(filters) == 1

    def test_redact_uses_default_policy(self):
        install_everywhere(RedactionPolicy(literals=["default-secret-value"]))
        assert "default-secret-value" not in redact("x default-secret-value y")
