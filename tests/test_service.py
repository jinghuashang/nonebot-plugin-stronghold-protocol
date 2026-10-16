"""服务层：日期口径、绑定流程、未绑定时的报错。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from nonebot_plugin_stronghold_protocol.bridge import BridgeError
from nonebot_plugin_stronghold_protocol.config import Config
from nonebot_plugin_stronghold_protocol.models import BindChallenge, BindResult, DailyReport, PlayerProfile
from nonebot_plugin_stronghold_protocol.service import StrongholdService, resolve_tz
from nonebot_plugin_stronghold_protocol.storage import BindingStore


class StubClient:
    """替掉真 HTTP：只验证服务层的编排逻辑。"""

    def __init__(self):
        self.calls = []
        self.challenge = BindChallenge(request_id="b_1", masked_email="3****@qq.com", expires_at=0)
        self.result = BindResult(player_id="a_0000000001", name="凯尔希", email="3****@qq.com")
        self.verify_error = None

    async def bind_request(self, email, qq=""):
        self.calls.append(("request", email, qq))
        return self.challenge

    async def bind_verify(self, request_id, code, qq=""):
        self.calls.append(("verify", request_id, code, qq))
        if self.verify_error:
            raise BridgeError(self.verify_error)
        return self.result

    async def player(self, *, player_id=None, name=None):
        self.calls.append(("player", player_id, name))
        return PlayerProfile(player_id=player_id or "a_1", name=name or "阿米娅")

    async def daily(self, day=None):
        self.calls.append(("daily", day))
        return DailyReport(date=(day or date(2026, 10, 15)).isoformat())


def make_service(tmp_path, **cfg) -> StrongholdService:
    config = Config(stronghold_api_token="t", **cfg)
    return StrongholdService(config, store=BindingStore(tmp_path / "bindings.json"))


def test_resolve_tz_falls_back_when_unknown():
    assert resolve_tz("Asia/Shanghai") is not None
    assert resolve_tz("Not/AZone") is not None  # 非法时区名不能抛异常
    assert resolve_tz("") is not None


def test_report_date_uses_configured_tz(tmp_path):
    service = make_service(tmp_path, stronghold_report_tz="Asia/Shanghai")
    assert service.report_date() == datetime.now(service.tz).date()


def test_parse_date(tmp_path):
    service = make_service(tmp_path)
    today = date(2026, 10, 15)
    assert service.parse_date("2026-10-01", today) == date(2026, 10, 1)
    assert service.parse_date("10-01", today) == date(2026, 10, 1)
    assert service.parse_date("昨天", today) == date(2026, 10, 14)
    assert service.parse_date("yesterday", today) == date(2026, 10, 14)
    assert service.parse_date("今天", today) == today
    assert service.parse_date("", today) is None
    assert service.parse_date("昨天下午", today) is None
    assert service.parse_date("2026-13-99", today) is None


async def test_profile_of_requires_binding(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(BridgeError) as err:
        await service.profile_of("10001")
    assert err.value.code == "bind_required"


async def test_bind_flow(tmp_path):
    service = make_service(tmp_path)
    service.client = StubClient()

    challenge = await service.start_bind("10001", "10001@qq.com")
    assert challenge.request_id == "b_1"
    assert service.client.calls[-1] == ("request", "10001@qq.com", "10001")
    # 待验证请求记在本地，回码时用它
    pending = service.store.peek_pending("10001")
    assert pending is not None and pending.request_id == "b_1"

    result = await service.finish_bind("10001", "123456")
    assert result.player_id == "a_0000000001"
    assert service.client.calls[-1] == ("verify", "b_1", "123456", "10001")
    assert service.store.peek_pending("10001") is None  # 用掉了

    binding = service.binding("10001")
    assert binding.player_id == "a_0000000001"
    assert binding.name == "凯尔希"

    # 绑好之后 /战绩 走 playerId
    await service.profile_of("10001")
    assert service.client.calls[-1] == ("player", "a_0000000001", None)


async def test_verify_without_pending_request(tmp_path):
    service = make_service(tmp_path)
    service.client = StubClient()
    with pytest.raises(BridgeError) as err:
        await service.finish_bind("10001", "123456")
    assert err.value.code == "request_not_found"
    assert service.client.calls == []


async def test_wrong_code_can_be_retried(tmp_path):
    """打错一次码不该把待验证请求作废（服务端限 5 次，玩家可以接着试）。"""
    service = make_service(tmp_path)
    stub = StubClient()
    stub.verify_error = "code_invalid"
    service.client = stub
    await service.start_bind("10001", "a@qq.com")
    with pytest.raises(BridgeError) as err:
        await service.finish_bind("10001", "000000")
    assert err.value.code == "code_invalid"
    assert service.binding("10001") is None
    assert service.store.peek_pending("10001") is not None

    # 接着用正确的码就能绑上
    stub.verify_error = None
    result = await service.finish_bind("10001", "123456")
    assert result.player_id == "a_0000000001"
    assert service.store.peek_pending("10001") is None


async def test_expired_code_forces_a_new_request(tmp_path):
    service = make_service(tmp_path)
    stub = StubClient()
    stub.verify_error = "code_expired"
    service.client = stub
    await service.start_bind("10001", "a@qq.com")
    with pytest.raises(BridgeError) as err:
        await service.finish_bind("10001", "000000")
    assert err.value.code == "code_expired"
    assert service.store.peek_pending("10001") is None, "服务端已作废，本地也要丢掉"
    with pytest.raises(BridgeError) as err2:
        await service.finish_bind("10001", "123456")
    assert err2.value.code == "request_not_found"


async def test_unbind(tmp_path):
    service = make_service(tmp_path)
    service.client = StubClient()
    await service.start_bind("10001", "a@qq.com")
    await service.finish_bind("10001", "123456")
    assert service.binding("10001") is not None
    removed = await service.unbind("10001")
    assert removed.player_id == "a_0000000001"
    assert service.binding("10001") is None


async def test_daily_passes_the_day(tmp_path):
    service = make_service(tmp_path)
    service.client = StubClient()
    report = await service.daily(date(2026, 10, 1))
    assert report.date == "2026-10-01"
    assert service.client.calls[-1] == ("daily", date(2026, 10, 1))
