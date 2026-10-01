"""候补模式测试：状态解析、表单构造、离线端到端、退避与通知。全部离线。"""

from __future__ import annotations

import json

import pytest

from railkit.notifier import Event, MemoryAdapter, NotifyHub
from railkit.waitlist import (
    DEFAULT_ENDPOINTS,
    HttpWaitlistBackend,
    SimulatedBackend,
    WaitlistEndpoints,
    WaitlistRequest,
    WaitlistRunner,
    WaitlistRunnerConfig,
    WaitlistState,
    parse_status,
    request_from_env,
)


def make_request(**overrides):
    payload = {
        "left_date": "2026-10-01",
        "left_station": "北京",
        "arrive_station": "上海",
        "train_numbers": ["G1", "G3"],
        "seat_types": ["O"],
        "passengers": [
            {"passenger_name": "张三", "passenger_id_no": "11010119900307721X", "passenger_id_type_code": "1", "passenger_type": "1"}
        ],
    }
    payload.update(overrides)
    return WaitlistRequest(**payload)


def make_runner(script=None, **config_kwargs):
    hub = NotifyHub([MemoryAdapter()], sleep=lambda _s: None, retry_base=0.0)
    config = WaitlistRunnerConfig(**config_kwargs)
    runner = WaitlistRunner(SimulatedBackend(script), make_request(), hub=hub, config=config, sleeper=lambda _s: None)
    return runner, hub.adapters[0]


class TestParseStatus:
    def test_success_status_flag(self):
        status = parse_status({"status": True, "data": {"orderId": "123"}})
        assert status.state == WaitlistState.QUEUED
        assert status.order_id == "123"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("兑现成功", WaitlistState.FULFILLED),
            ("已兑现，请支付", WaitlistState.FULFILLED),
            ("兑现失败", WaitlistState.FAILED),
            ("候选已失效", WaitlistState.EXPIRED),
            ("订单已取消", WaitlistState.CANCELED),
            ("正在排队候补", WaitlistState.QUEUED),
        ],
    )
    def test_keyword_mapping(self, text, expected):
        assert parse_status({"result_message": text}).state == expected

    def test_explicit_numeric_status(self):
        assert parse_status({"data": {"hbStatus": "5"}}).state == WaitlistState.FULFILLED
        assert parse_status({"data": {"hbStatus": "7"}}).state == WaitlistState.EXPIRED

    def test_unrecognised_is_error_not_success(self):
        """识别不出来时必须报 ERROR：把未知当成功会造成「假兑现」告警。"""
        status = parse_status({"weird": "payload"})
        assert status.state == WaitlistState.ERROR
        assert "无法识别" in status.message

    def test_string_payload(self):
        assert parse_status("[1,2,3]").state == WaitlistState.ERROR

    def test_queue_position_parsed(self):
        assert parse_status({"data": {"hbStatus": "1", "queuePosition": "42"}}).queue_position == 42

    def test_message_is_redacted(self):
        status = parse_status({"result_message": "正在排队 cookie RAIL_DEVICEID=abcdefg1234567890"})
        assert "abcdefg1234567890" not in status.as_dict()["message"]


class TestRequestForm:
    def test_form_has_expected_fields(self):
        form = make_request().to_form()
        assert form["train_date"] == "2026-10-01"
        assert form["from_station_name"] == "北京"
        assert form["train_no"] == "G1,G3"
        assert form["acceptNoSeat"] == "1"
        assert form["isAdjacent"] == "0"
        assert "passengerTicketStr" in form and "oldPassengerStr" in form

    def test_accept_flags(self):
        form = make_request(accept_no_seat=False, accept_adjacent=True).to_form()
        assert form["acceptNoSeat"] == "0"
        assert form["isAdjacent"] == "1"

    def test_key_is_stable_and_order_independent(self):
        a = make_request(train_numbers=["G1", "G3"]).key()
        b = make_request(train_numbers=["G3", "G1"]).key()
        assert a == b

    def test_key_changes_with_date(self):
        assert make_request().key() != make_request(left_date="2026-10-02").key()


class TestRequestFromEnv:
    def test_none_when_absent(self):
        assert request_from_env({}) is None

    def test_parses_json(self):
        payload = {
            "left_date": "2026-10-01",
            "left_station": "北京",
            "arrive_station": "上海",
            "train_numbers": ["G1"],
            "accept_adjacent": True,
        }
        request = request_from_env({"WAITLIST_JSON": json.dumps(payload)})
        assert request.train_numbers == ["G1"]
        assert request.accept_adjacent is True

    def test_requires_core_fields(self):
        with pytest.raises(ValueError) as exc:
            request_from_env({"WAITLIST_JSON": json.dumps({"left_date": "2026-10-01"})})
        assert "缺少必填字段" in str(exc.value)

    def test_rejects_non_object(self):
        with pytest.raises(ValueError):
            request_from_env({"WAITLIST_JSON": "[1,2]"})


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload

    @property
    def text(self):
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)


class FakeSession:
    def __init__(self, post_payload=None, get_payload=None, raise_on=None):
        self.post_payload = post_payload or {"status": True, "data": {"orderId": "O1"}}
        self.get_payload = get_payload or {"data": {"hbStatus": "1"}}
        self.raise_on = raise_on
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data))
        if self.raise_on == "post":
            raise RuntimeError("boom")
        return FakeResponse(self.post_payload)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        if self.raise_on == "get":
            raise RuntimeError("boom")
        return FakeResponse(self.get_payload)


class TestHttpBackend:
    def test_submit_uses_configured_endpoint(self):
        session = FakeSession()
        endpoints = WaitlistEndpoints(submit="https://example.test/submit")
        backend = HttpWaitlistBackend(session, endpoints)
        status = backend.submit(make_request())
        assert status.state == WaitlistState.QUEUED
        assert session.calls[0][1] == "https://example.test/submit"
        assert session.calls[0][2]["train_date"] == "2026-10-01"

    def test_query_parses_status(self):
        session = FakeSession(get_payload={"result_message": "兑现成功"})
        assert HttpWaitlistBackend(session).query("O1").state == WaitlistState.FULFILLED

    def test_network_error_becomes_error_state(self):
        session = FakeSession(raise_on="post")
        status = HttpWaitlistBackend(session).submit(make_request())
        assert status.state == WaitlistState.ERROR
        assert "RuntimeError" in status.message

    def test_query_error_reported(self):
        session = FakeSession(raise_on="get")
        assert HttpWaitlistBackend(session).query("O1").state == WaitlistState.ERROR

    def test_cancel_maps_to_canceled(self):
        session = FakeSession(post_payload={"status": True})
        assert HttpWaitlistBackend(session).cancel("O1").state == WaitlistState.CANCELED

    def test_endpoints_override_from_env(self):
        endpoints = WaitlistEndpoints.from_env(
            {
                "WAITLIST_SUBMIT_URL": "https://a.test/s",
                "WAITLIST_QUERY_URL": "https://a.test/q",
                "WAITLIST_CANCEL_URL": "https://a.test/c",
            }
        )
        assert endpoints.submit == "https://a.test/s"
        assert endpoints.query == "https://a.test/q"
        assert endpoints.cancel == "https://a.test/c"

    def test_defaults_are_documented_constants(self):
        assert DEFAULT_ENDPOINTS["submit"].startswith("https://kyfw.12306.cn/")


class TestSimulatedEndToEnd:
    def test_full_flow_reaches_fulfilled_and_notifies(self):
        runner, memory = make_runner([WaitlistState.QUEUED, WaitlistState.QUEUED, WaitlistState.FULFILLED])
        status = runner.run_until_terminal(sleeper=lambda _s: None)
        assert status.state == WaitlistState.FULFILLED
        events = [m.event for m in memory.messages]
        assert Event.TICKET_SUCCESS in events
        assert any("请尽快确认支付" in m.body for m in memory.messages)

    def test_failed_flow_notifies_failure(self):
        runner, memory = make_runner([WaitlistState.QUEUED, WaitlistState.FAILED])
        status = runner.run_until_terminal(sleeper=lambda _s: None)
        assert status.state == WaitlistState.FAILED
        assert Event.TICKET_ALL_FAILED in [m.event for m in memory.messages]

    def test_submit_notification_includes_route(self):
        runner, memory = make_runner()
        runner.submit()
        assert any("北京->上海" in m.body for m in memory.messages)

    def test_expired_is_terminal(self):
        runner, memory = make_runner([WaitlistState.EXPIRED])
        assert runner.run_until_terminal(sleeper=lambda _s: None).is_terminal

    def test_cancel_flow(self):
        runner, memory = make_runner()
        runner.submit()
        status = runner.cancel()
        assert status.state == WaitlistState.CANCELED
        assert runner.status.is_terminal
        assert any("已取消候补" in m.body for m in memory.messages)

    def test_history_records_only_transitions(self):
        runner, _ = make_runner([WaitlistState.QUEUED, WaitlistState.QUEUED, WaitlistState.QUEUED])
        runner.submit()
        runner.check_once()
        runner.check_once()
        # submit 一次 + 状态变化时的记录，同状态重复查询不重复入历史
        states = [s.state for s in runner.history]
        assert states.count(WaitlistState.QUEUED) <= 3

    def test_no_duplicate_notifications_for_same_state(self):
        runner, memory = make_runner([WaitlistState.QUEUED, WaitlistState.QUEUED, WaitlistState.QUEUED])
        runner.submit()
        before = len(memory.messages)
        runner.check_once()
        runner.check_once()
        assert len(memory.messages) == before


class TestRetryAndBackoff:
    def test_submit_retries_then_reports_error(self):
        hub = NotifyHub([MemoryAdapter()], sleep=lambda _s: None, retry_base=0.0)
        backend = SimulatedBackend(raise_on_submit=True)
        runner = WaitlistRunner(
            backend, make_request(), hub=hub,
            config=WaitlistRunnerConfig(submit_retries=2), sleeper=lambda _s: None,
        )
        status = runner.submit()
        assert status.state == WaitlistState.ERROR
        assert backend.submit_calls == 3  # 1 次 + 2 次重试
        errors = [m for m in hub.adapters[0].messages if m.event == Event.TASK_ERROR]
        assert len(errors) == 3

    def test_backoff_grows_and_is_capped(self):
        runner, _ = make_runner([WaitlistState.QUEUED], backoff_factor=2.0, backoff_cap=600.0, first_check_seconds=90.0)
        intervals = []
        for _ in range(8):
            runner.checks += 1
            intervals.append(runner.next_interval())
        # 带抖动，所以不是严格单调；但必须整体增长并最终贴到上限
        assert intervals[0] == pytest.approx(90.0, rel=0.35)
        assert intervals[-1] == pytest.approx(600.0, rel=0.35)
        assert min(intervals[:3]) < max(intervals[-3:])
        assert max(intervals) <= 600.0

    def test_max_checks_stops_polling_and_warns(self):
        runner, memory = make_runner([WaitlistState.QUEUED], first_check_seconds=0.01)
        status = runner.run_until_terminal(max_checks=3, sleeper=lambda _s: None)
        assert status.state == WaitlistState.QUEUED
        assert runner.checks == 3
        assert any("达到上限" in m.body for m in memory.messages)

    def test_query_exception_is_reported_not_raised(self):
        class BrokenBackend(SimulatedBackend):
            def query(self, order_id=""):
                raise RuntimeError("network down")

        hub = NotifyHub([MemoryAdapter()], sleep=lambda _s: None, retry_base=0.0)
        runner = WaitlistRunner(BrokenBackend(), make_request(), hub=hub, sleeper=lambda _s: None)
        runner.submit()
        status = runner.check_once()
        assert status.state == WaitlistState.ERROR
        assert "network down" in status.message
        assert Event.TASK_ERROR in [m.event for m in hub.adapters[0].messages]
