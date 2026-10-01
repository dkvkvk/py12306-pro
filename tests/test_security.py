"""登录态加密与通知层测试（规格书 3.2 / 4 / 9）。全部离线。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.notifier import (
    Adapter,
    BarkAdapter,
    ConsoleAdapter,
    DingTalkAdapter,
    Event,
    MemoryAdapter,
    Message,
    NotifyHub,
    SendResult,
    ServerChanAdapter,
    WebhookAdapter,
    make_notifier,
)
from core.runtime_state import (
    LoginStateStore,
    StateEncryptionUnavailable,
    decrypt_bytes,
    derive_key,
    encrypt_bytes,
    harden_dir,
    is_encrypted,
    permission_report,
    secure_write,
)

SECRET = "unit-test-enc-key-0123456789"


# --- 加密原语 -------------------------------------------------------------


class TestCrypto:
    def test_round_trip(self):
        payload = {"cookies": {"RAIL_DEVICEID": "abc", "JSESSIONID": "def"}}
        blob = encrypt_bytes(json.dumps(payload).encode("utf-8"), SECRET)
        assert decrypt_bytes(blob, SECRET) == json.dumps(payload).encode("utf-8")

    def test_ciphertext_does_not_contain_plaintext(self):
        blob = encrypt_bytes(b"super-secret-cookie-value", SECRET)
        assert b"super-secret-cookie-value" not in blob

    def test_wrong_key_fails(self):
        blob = encrypt_bytes(b"payload", SECRET)
        with pytest.raises(Exception):
            decrypt_bytes(blob, "another-key-entirely")

    def test_tampered_ciphertext_is_rejected(self):
        """AES-GCM 必须能识别被改过的文件。"""
        blob = bytearray(encrypt_bytes(b"payload-value", SECRET))
        blob[-1] ^= 0x01
        with pytest.raises(Exception):
            decrypt_bytes(bytes(blob), SECRET)

    def test_non_magic_blob_rejected(self):
        with pytest.raises(ValueError):
            decrypt_bytes(b'{"cookies": {}}', SECRET)

    def test_same_plaintext_gives_different_ciphertext(self):
        """随机 salt/nonce：同样的内容两次加密结果必须不同。"""
        first = encrypt_bytes(b"payload", SECRET)
        second = encrypt_bytes(b"payload", SECRET)
        assert first != second

    def test_derive_key_is_stable_and_32_bytes(self):
        salt = b"0123456789abcdef"
        assert derive_key(SECRET, salt) == derive_key(SECRET, salt)
        assert len(derive_key(SECRET, salt)) == 32

    def test_derive_key_differs_by_salt(self):
        assert derive_key(SECRET, b"0123456789abcdef") != derive_key(SECRET, b"fedcba9876543210")


# --- 落盘 -----------------------------------------------------------------


class TestLoginStateStore:
    def _store(self, tmp_path: Path, **kwargs) -> LoginStateStore:
        return LoginStateStore(tmp_path / "user", kwargs.pop("secret", SECRET), **kwargs)

    def test_save_and_load_round_trip(self, tmp_path):
        store = self._store(tmp_path)
        payload = {"cookies": {"RAIL_DEVICEID": "abc"}}
        path = store.save("acct", payload)
        assert path.name.endswith(".json.enc")
        assert is_encrypted(path)
        assert store.load("acct") == payload

    def test_saved_file_is_not_plaintext(self, tmp_path):
        store = self._store(tmp_path)
        path = store.save("acct", {"cookies": {"JSESSIONID": "verysecret"}})
        assert "verysecret" not in path.read_text(encoding="utf-8", errors="ignore")

    def test_refuses_plaintext_without_key(self, tmp_path):
        """失败关闭：没有 RUNTIME_ENC_KEY 就不许明文落盘。"""
        store = LoginStateStore(tmp_path / "user", None)
        with pytest.raises(StateEncryptionUnavailable):
            store.save("acct", {"cookies": {}})

    def test_plaintext_allowed_when_explicitly_opted_in(self, tmp_path):
        store = LoginStateStore(tmp_path / "user", None, allow_plaintext=True)
        path = store.save("acct", {"cookies": {}})
        assert path.name.endswith(".json")
        assert store.load("acct") == {"cookies": {}}

    def test_load_encrypted_without_key_fails(self, tmp_path):
        self._store(tmp_path).save("acct", {"a": 1})
        store = LoginStateStore(tmp_path / "user", None)
        with pytest.raises(StateEncryptionUnavailable):
            store.load("acct")

    def test_load_missing_returns_none(self, tmp_path):
        assert self._store(tmp_path).load("nope") is None

    def test_encrypted_save_removes_legacy_plaintext(self, tmp_path):
        store = self._store(tmp_path)
        legacy = store.legacy_path_for("acct")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"cookies": {"JSESSIONID": "old"}}', encoding="utf-8")
        store.save("acct", {"cookies": {"JSESSIONID": "new"}})
        assert not legacy.exists()
        assert store.load("acct")["cookies"]["JSESSIONID"] == "new"

    def test_name_is_sanitised(self, tmp_path):
        store = self._store(tmp_path)
        path = store.save("../../etc/passwd", {"a": 1})
        assert path.parent == store.root

    def test_list_and_purge(self, tmp_path):
        store = self._store(tmp_path)
        store.save("a", {"x": 1})
        store.save("b", {"x": 2})
        assert {item.name for item in store.list_states()} == {"a", "b"}
        removed = store.purge(["a"])
        assert len(removed) == 1
        assert {item.name for item in store.list_states()} == {"b"}
        assert len(store.purge()) == 1
        assert store.list_states() == []

    def test_purge_all_removes_directory(self, tmp_path):
        store = self._store(tmp_path)
        store.save("a", {"x": 1})
        store.purge()
        assert not store.root.exists()

    def test_purge_never_touches_non_state_files(self, tmp_path):
        """runtime/user 下还有上游用来保住目录的 .gitignore，purge 不能连它一起删。"""
        root = tmp_path / "user"
        root.mkdir(parents=True)
        placeholder = root / ".gitignore"
        placeholder.write_text("!*\n", encoding="utf-8")
        keep = root / "notes.md"
        keep.write_text("x", encoding="utf-8")

        store = LoginStateStore(root, SECRET)
        store.save("acct", {"cookies": {}})
        removed = store.purge()

        assert {path.name for path in removed} == {"acct.json.enc"}
        assert placeholder.exists()
        assert keep.exists()

    def test_purge_keeps_directory_when_only_placeholders_remain(self, tmp_path):
        root = tmp_path / "user"
        root.mkdir(parents=True)
        placeholder = root / ".gitignore"
        placeholder.write_text("!*\n", encoding="utf-8")

        store = LoginStateStore(root, SECRET)
        store.save("acct", {"cookies": {}})
        store.purge()

        assert root.is_dir()
        assert placeholder.exists()

    def test_list_states_ignores_placeholders_and_temp(self, tmp_path):
        root = tmp_path / "user"
        root.mkdir(parents=True)
        (root / ".gitignore").write_text("!*\n", encoding="utf-8")
        (root / "acct.json.enc.tmp123").write_text("partial", encoding="utf-8")
        (root / "readme.txt").write_text("hi", encoding="utf-8")

        store = LoginStateStore(root, SECRET)
        store.save("acct", {"cookies": {}})

        assert [item.name for item in store.list_states()] == ["acct"]

    def test_audit_reports_encryption_and_permissions(self, tmp_path):
        store = self._store(tmp_path)
        store.save("acct", {"x": 1})
        entry = store.audit()[0]
        assert entry["encrypted"] is True
        assert entry["name"] == "acct"

    def test_from_config_uses_config_key(self, base_env, tmp_path):
        from core.config import build_config

        config = build_config(base_env, base_dir=tmp_path)
        store = LoginStateStore.from_config(config)
        assert store.secret == base_env["RUNTIME_ENC_KEY"]
        assert store.root == config.paths.state_dir / "user"


class TestPermissions:
    def test_secure_write_restricts_posix_permissions(self, tmp_path):
        target = tmp_path / "secret.json.enc"
        secure_write(target, b"data")
        assert target.read_bytes() == b"data"
        if os.name == "posix":
            mode = os.stat(target).st_mode & 0o777
            assert mode == 0o600

    def test_secure_write_is_atomic_and_leaves_no_temp(self, tmp_path):
        target = tmp_path / "file"
        secure_write(target, b"one")
        secure_write(target, b"two")
        assert target.read_bytes() == b"two"
        leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert leftovers == []

    def test_harden_dir_creates(self, tmp_path):
        target = tmp_path / "deep" / "runtime"
        harden_dir(target)
        assert target.is_dir()

    def test_permission_report_shape(self, tmp_path):
        target = tmp_path / "x"
        assert permission_report(target)["exists"] is False
        target.write_text("y", encoding="utf-8")
        report = permission_report(target)
        assert report["exists"] is True
        assert "mode" in report


# --- 通知适配器 -----------------------------------------------------------


class FakeTransport:
    """记录调用并以固定状态码应答。"""

    def __init__(self, code: int = 200, body: str = "{}") -> None:
        self.code = code
        self.body = body
        self.calls = []

    def __call__(self, url, payload, headers=None, timeout=10.0):
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return self.code, self.body


def msg(event: str = Event.RISK_CONTROL) -> Message:
    return Message(event=event, title="t", body="b")


class TestAdapters:
    def test_webhook_posts_json(self):
        transport = FakeTransport()
        adapter = WebhookAdapter("https://hook.example/x", headers={"X-K": "v"}, transport=transport)
        assert adapter.send(msg()).ok is True
        call = transport.calls[0]
        assert call["url"] == "https://hook.example/x"
        assert call["payload"]["event"] == Event.RISK_CONTROL
        assert call["headers"] == {"X-K": "v"}

    def test_webhook_requires_url(self):
        with pytest.raises(ValueError):
            WebhookAdapter("")

    def test_webhook_reports_http_error(self):
        adapter = WebhookAdapter("https://hook.example/x", transport=FakeTransport(500, "boom"))
        assert adapter.send(msg()).ok is False

    def test_dingtalk_signature_is_added(self):
        transport = FakeTransport()
        adapter = DingTalkAdapter("https://oapi.dingtalk.com/robot/send?access_token=t", secret="SEC", transport=transport)
        adapter.send(msg())
        url = transport.calls[0]["url"]
        assert "timestamp=" in url and "sign=" in url

    def test_dingtalk_without_secret_keeps_url(self):
        transport = FakeTransport()
        adapter = DingTalkAdapter("https://oapi.dingtalk.com/robot/send?access_token=t", transport=transport)
        adapter.send(msg())
        assert "sign=" not in transport.calls[0]["url"]

    def test_dingtalk_surfaces_business_error(self):
        adapter = DingTalkAdapter("https://x/y", transport=FakeTransport(200, '{"errcode": 310000, "errmsg": "keywords"}'))
        result = adapter.send(msg())
        assert result.ok is False
        assert "310000" in result.detail

    def test_dingtalk_keyword_is_prefixed(self):
        transport = FakeTransport()
        DingTalkAdapter("https://x/y", keyword="[12306] ", transport=transport).send(msg())
        assert transport.calls[0]["payload"]["markdown"]["title"].startswith("[12306]")

    def test_serverchan_success(self):
        transport = FakeTransport(200, '{"code": 0}')
        adapter = ServerChanAdapter("SCTKEY", transport=transport)
        assert adapter.send(msg()).ok is True
        assert "SCTKEY" in transport.calls[0]["url"]

    def test_bark_sets_priority_by_severity(self):
        transport = FakeTransport()
        adapter = BarkAdapter("https://api.day.app/key", transport=transport)
        adapter.send(Message(event=Event.RISK_CONTROL, title="t", body="b", severity="high"))
        assert transport.calls[0]["payload"]["level"] == "timeSensitive"
        adapter.send(Message(event=Event.NO_TICKET, title="t", body="b", severity="low"))
        assert transport.calls[1]["payload"]["level"] == "active"

    def test_console_filters_by_severity(self):
        lines = []
        adapter = ConsoleAdapter(out=lines.append, min_severity="high")
        adapter.send(Message(event=Event.NO_TICKET, title="t", body="b", severity="low"))
        assert lines == []
        adapter.send(Message(event=Event.RISK_CONTROL, title="t", body="b", severity="high"))
        assert lines and "RISK_CONTROL" in lines[0]

    def test_memory_adapter_records(self):
        adapter = MemoryAdapter()
        adapter.send(msg())
        assert len(adapter.messages) == 1

    def test_event_filtering(self):
        adapter = MemoryAdapter()
        adapter.events = (Event.TICKET_SUCCESS,)
        assert adapter.wants(Event.TICKET_SUCCESS) is True
        assert adapter.wants(Event.RISK_CONTROL) is False

    def test_message_markdown_includes_extra(self):
        rendered = Message(event="X", title="t", body="b", extra={"key": "k"}).to_markdown()
        assert "key: k" in rendered


class FlakyAdapter(Adapter):
    name = "flaky"

    def __init__(self, fail_times: int = 99) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    def send(self, message, timeout: float = 10.0) -> SendResult:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            return SendResult(False, "boom")
        return SendResult(True, "ok")


class ExplodingAdapter(Adapter):
    name = "exploding"

    def send(self, message, timeout: float = 10.0) -> SendResult:
        raise RuntimeError("adapter is broken")


class TestNotifyHub:
    def _hub(self, *adapters) -> NotifyHub:
        return NotifyHub(list(adapters), sleep=lambda _s: None, retry_base=0.0)

    def test_retries_then_succeeds(self):
        flaky = FlakyAdapter(fail_times=1)
        hub = self._hub(flaky)
        results = hub.notify("SYSTEM", "hi")
        assert results["flaky"]["ok"] is True
        assert results["flaky"]["attempts"] == 2

    def test_one_broken_adapter_does_not_block_others(self):
        good = MemoryAdapter()
        hub = self._hub(ExplodingAdapter(), good)
        results = hub.notify("SYSTEM", "hi")
        assert results["exploding"]["ok"] is False
        assert results["memory"]["ok"] is True
        assert len(good.messages) == 1

    def test_exception_from_adapter_is_captured(self):
        hub = self._hub(ExplodingAdapter())
        result = hub.notify("SYSTEM", "hi")["exploding"]
        assert result["ok"] is False
        assert "RuntimeError" in result["detail"]

    def test_failed_adapter_enters_cooldown(self):
        flaky = FlakyAdapter(fail_times=999)
        hub = self._hub(flaky)
        for _ in range(3):
            hub.notify("SYSTEM", "hi")
        calls_before = flaky.attempts
        results = hub.notify("SYSTEM", "hi")
        assert "cooldown" in results["flaky"].get("skipped", "")
        assert flaky.attempts == calls_before

    def test_event_is_normalised(self):
        memory = MemoryAdapter()
        self._hub(memory).notify("risk_control", "hit")
        assert memory.messages[0].event == Event.RISK_CONTROL

    def test_severity_mapping(self):
        memory = MemoryAdapter()
        self._hub(memory).notify("TICKET_SUCCESS", "win")
        assert memory.messages[0].severity == "info"
        hub = self._hub(memory)
        hub.notify("RISK_CONTROL", "hit")
        assert memory.messages[-1].severity == "high"

    def test_message_is_redacted_before_send(self):
        memory = MemoryAdapter()
        self._hub(memory).notify("SYSTEM", "cookie JSESSIONID=abc123XYZdef456")
        assert "abc123XYZdef456" not in memory.messages[-1].body

    def test_history_is_recorded(self):
        hub = self._hub(MemoryAdapter())
        hub.notify("SYSTEM", "one")
        assert len(hub.history) == 1
        assert hub.history[0]["event"] == "SYSTEM"

    def test_status_reports_per_adapter(self):
        hub = self._hub(MemoryAdapter())
        hub.notify("SYSTEM", "one")
        assert hub.status()["memory"]["success"] == 1

    def test_make_notifier_bridges_to_breaker_callback(self):
        memory = MemoryAdapter()
        notify = make_notifier(self._hub(memory))
        notify("risk_control", "命中风控", {"key": "task-1"})
        assert memory.messages[0].event == Event.RISK_CONTROL
        assert memory.messages[0].extra["key"] == "task-1"

    def test_default_titles(self):
        memory = MemoryAdapter()
        self._hub(memory).notify("LOGIN_EXPIRED", "")
        assert "登录态" in memory.messages[0].title

    def test_from_env_falls_back_to_console(self):
        hub = NotifyHub.from_env({"NOTIFY_ADAPTERS": ""})
        assert [a.name for a in hub.adapters] == ["console"]

    def test_from_env_skips_unknown_adapter(self):
        hub = NotifyHub.from_env({"NOTIFY_ADAPTERS": "console,nope"})
        assert [a.name for a in hub.adapters] == ["console"]

    def test_from_env_skips_misconfigured_adapter(self, caplog):
        hub = NotifyHub.from_env({"NOTIFY_ADAPTERS": "dingtalk,console"})
        assert [a.name for a in hub.adapters] == ["console"]

    def test_from_env_builds_dingtalk(self):
        hub = NotifyHub.from_env(
            {"NOTIFY_ADAPTERS": "dingtalk", "DINGTALK_WEBHOOK": "https://x/y", "DINGTALK_SECRET": "s"}
        )
        assert [a.name for a in hub.adapters] == ["dingtalk"]

    def test_from_env_never_raises_with_nothing_configured(self):
        hub = NotifyHub.from_env({"NOTIFY_ADAPTERS": "serverchan"})
        assert [a.name for a in hub.adapters] == ["console"]
