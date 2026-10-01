"""pytest 公共夹具：所有测试都不依赖网络、Redis、真实时间。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def disable_upstream_config_watcher(monkeypatch):
    """上游 Config 会在构造时起一个后台线程轮询配置文件。

    这个线程在测试里只会带来麻烦（文件不存在就一直 poll、退出时挂住 pytest），
    所以在整个测试会话里统一关掉。
    """
    try:
        from py12306.config import Config
    except Exception:  # 依赖缺失时跳过，不影响其它测试
        return
    monkeypatch.setattr(Config, "watch_file_change", lambda self: None)
    monkeypatch.delattr(Config, "__it__", raising=False)


@pytest.fixture(autouse=True)
def block_upstream_network(monkeypatch):
    """测试必须离线：上游 Query 初始化时会真的去请求 12306。

    Query.__init__ -> request_device_id() -> GET 12306 取设备指纹，
    离线环境下会一直阻塞（测试挂死就是这么来的）。
    """
    try:
        from py12306.query.query import Query
    except Exception:
        return
    monkeypatch.setattr(Query, "request_device_id", lambda self, force_renew=False: None)
    monkeypatch.setattr(Query, "request_device_id2", lambda self: None)
    monkeypatch.setattr(Query, "get_query_api_type", classmethod(lambda cls: "leftTicket/queryZ"))


class FakeClock:
    """可手动推进的单调时钟，用于确定性地测试退避/熔断时序。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


class FakeHTTPError(Exception):
    def __init__(self, code: int = 500) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


class FakeRequestException(Exception):
    pass


class FakeTimeout(FakeRequestException):
    pass


class FakeConnectionError(FakeRequestException):
    pass


class FakeSSLError(FakeRequestException):
    pass


@pytest.fixture()
def fake_errors():
    """模拟 requests 的异常层级，避免测试依赖 requests 是否安装。"""
    return {
        "timeout": FakeTimeout("read timeout"),
        "connection": FakeConnectionError("connection aborted"),
        "ssl": FakeSSLError("certificate verify failed"),
        "request": FakeRequestException("bad request"),
        "http500": FakeHTTPError(500),
        "weird": ValueError("something else"),
    }


@pytest.fixture()
def base_env(tmp_path: Path) -> dict:
    """一套能通过校验的最小生产配置（无网络需求）。"""
    return {
        "USER_ACCOUNTS_JSON": '[{"username":"tester","password":"s3cret-pwd","login_type":"qr"}]',
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "JWT_SECRET_KEY": "x" * 48,
        "RUNTIME_ENC_KEY": "unit-test-enc-key-0123456789",
        "QUERY_INTERVAL": "4",
        "RUNTIME_DIR": str(tmp_path / "runtime"),
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "NOTIFY_ADAPTERS": "memory",
        "WEB_BIND": "127.0.0.1",
    }
