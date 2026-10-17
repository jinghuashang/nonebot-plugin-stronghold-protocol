"""定时推送：11:00 的算法、跨日、配置非法时的兜底、群发失败不影响其他群。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nonebot_plugin_stronghold_protocol import scheduler as scheduler_mod
from nonebot_plugin_stronghold_protocol.config import Config
from nonebot_plugin_stronghold_protocol.models import DailyReport, DailySummary
from nonebot_plugin_stronghold_protocol.scheduler import (
    DailyReportScheduler,
    MonthlyReportScheduler,
    WeeklyReportScheduler,
    parse_hhmm,
)
from nonebot_plugin_stronghold_protocol.service import StrongholdService
from nonebot_plugin_stronghold_protocol.storage import BindingStore

TZ = ZoneInfo("Asia/Shanghai")


class StubService(StrongholdService):
    def __init__(self, tmp_path, **cfg):
        super().__init__(Config(**cfg), store=BindingStore(tmp_path / "bindings.json"))
        self.daily_calls = 0
        self.error = None

    async def daily(self, day=None):
        self.daily_calls += 1
        if self.error:
            raise self.error
        return DailyReport(
            date="2026-10-15",
            summary=DailySummary(matches=2, finished=2, victories=1, defeats=1, gold_spent=100),
        )


class StubBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_group_msg(self, group_id, message):
        if group_id in self.fail_for:
            raise RuntimeError("boom")
        self.sent.append((group_id, message))


def test_parse_hhmm():
    assert parse_hhmm("11:00") == (11, 0)
    assert parse_hhmm("0:00") == (0, 0)
    assert parse_hhmm(" 23:59 ") == (23, 59)
    assert parse_hhmm("24:00") is None
    assert parse_hhmm("11:60") is None
    assert parse_hhmm("11") is None
    assert parse_hhmm("") is None
    assert parse_hhmm("上午十一点") is None


def test_next_fire_at_is_strictly_future(tmp_path):
    service = StubService(tmp_path, stronghold_report_time="11:00", stronghold_report_tz="Asia/Shanghai")
    sched = DailyReportScheduler(service, service.config)

    before = datetime(2026, 10, 15, 10, 30, tzinfo=TZ)
    assert sched.next_fire_at(before) == datetime(2026, 10, 15, 11, 0, tzinfo=TZ)

    exact = datetime(2026, 10, 15, 11, 0, tzinfo=TZ)
    assert sched.next_fire_at(exact) == datetime(2026, 10, 16, 11, 0, tzinfo=TZ), "刚好 11:00 要排到明天，不能立刻重推"

    after = datetime(2026, 10, 15, 23, 59, tzinfo=TZ)
    assert sched.next_fire_at(after) == datetime(2026, 10, 16, 11, 0, tzinfo=TZ)

    midnight = datetime(2026, 10, 15, 0, 1, tzinfo=TZ)
    assert sched.next_fire_at(midnight) == datetime(2026, 10, 15, 11, 0, tzinfo=TZ)


def test_invalid_time_falls_back_to_23(tmp_path):
    service = StubService(tmp_path, stronghold_report_time="晚上十一点")
    sched = DailyReportScheduler(service, service.config)
    assert sched.time_of_day == (23, 0)
    assert sched.time_of_day == (23, 0)  # 只警告一次，行为稳定


async def test_fire_renders_and_sends(tmp_path, monkeypatch):
    service = StubService(tmp_path, stronghold_report_time="11:00", stronghold_report_groups=[123456789, 987654321])
    bot = StubBot(fail_for=[987654321])
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": bot})
    sched = DailyReportScheduler(service, service.config)

    results = await sched.fire()
    assert service.daily_calls == 1
    assert len(bot.sent) == 1
    group_id, message = bot.sent[0]
    assert group_id == 123456789
    assert "🏰 今日战报 · 2026-10-15" in message
    assert "对局 2 局 · 胜 1 / 负 1 · 胜率 50.0%" in message
    assert any("987654321 发送失败" in r for r in results)
    assert any("123456789 已发送" in r for r in results)


async def test_fire_reports_bridge_errors_without_raising(tmp_path, monkeypatch):
    from nonebot_plugin_stronghold_protocol.bridge import BridgeError

    service = StubService(tmp_path, stronghold_report_groups=[1])
    service.error = BridgeError("unreachable")
    bot = StubBot()
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": bot})
    sched = DailyReportScheduler(service, service.config)

    results = await sched.fire()
    assert results == ["取数失败"]
    assert bot.sent == []


async def test_broadcast_without_bots(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {})
    assert await scheduler_mod.broadcast("hi", [1, 2]) == ["没有已连接的机器人，跳过推送"]


async def test_start_only_skips_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": StubBot()})

    service = StubService(tmp_path, stronghold_report_enabled=False, stronghold_report_groups=[1])
    sched = DailyReportScheduler(service, service.config)
    sched.start()
    assert sched._task is None  # noqa: SLF001 - 断言「没有起后台任务」最直接的方式

    # 没配群也要起循环：管理员可能今天就往群里发「日报 开启」。
    service2 = StubService(tmp_path, stronghold_report_enabled=True, stronghold_report_groups=[])
    sched2 = DailyReportScheduler(service2, service2.config)
    sched2.start()
    assert sched2._task is not None  # noqa: SLF001
    await sched2.stop()


async def test_fire_follows_group_toggles_at_runtime(tmp_path, monkeypatch):
    bot = StubBot()
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": bot})
    service = StubService(tmp_path, stronghold_report_time="11:00", stronghold_report_groups=[111])
    sched = DailyReportScheduler(service, service.config)

    await sched.fire()
    assert [group for group, _ in bot.sent] == [111]

    service.set_report_group(111, False, by="10001")  # 群里「日报 关闭」
    bot.sent.clear()
    await sched.fire()
    assert bot.sent == []

    service.set_report_group(222, True, by="10001")  # 另一个群「日报 开启」
    await sched.fire()
    assert [group for group, _ in bot.sent] == [222]


async def test_fire_without_groups_skips_the_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": StubBot()})
    service = StubService(tmp_path, stronghold_report_groups=[])
    sched = DailyReportScheduler(service, service.config)

    assert await sched.fire() == []
    assert service.daily_calls == 0, "没人订阅就不该去打服务端接口"


async def test_start_and_stop_a_real_task(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": StubBot()})
    service = StubService(tmp_path, stronghold_report_time="23:59", stronghold_report_groups=[1])
    sched = DailyReportScheduler(service, service.config)
    sched.start()
    assert sched._task is not None  # noqa: SLF001
    assert sched.next_fire_at() - datetime.now(service.tz) <= timedelta(days=1)
    await sched.stop()
    assert sched._task is None  # noqa: SLF001
    assert service.daily_calls == 0, "还没到点不能推"

async def test_broadcast_prefers_image_and_falls_back(tmp_path, monkeypatch):
    """有图就发图；某个群发图失败时该群回退文本；没有图就直接文本。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter, Bot

    from nonebot_plugin_stronghold_protocol import scheduler as sched

    class Bot2(Bot):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = []

        async def call_api(self, api, **data):
            message = data.get("message")
            is_image = bool(message) and getattr(message[0], "type", "") == "image"
            if api.endswith("send_group_msg") and is_image and data.get("group_id") == 2:
                raise RuntimeError("发图失败")
            self.calls.append((api, data))
            return {"message_id": 1}

    bot = Bot2(Adapter(nonebot.get_driver()), "12345")
    monkeypatch.setattr(sched, "get_bots", lambda: {"1": bot})

    results = await sched.broadcast("文本版", [1, 2], image=b"\xff\xd8\xff\xe0jpg")
    kinds = [(d.get("group_id"), getattr(d["message"][0], "type", "text")) for _, d in bot.calls]
    assert kinds == [(1, "image"), (2, "text")], kinds
    assert any("（图片）" in r for r in results) and any("1 已发送" in r for r in results)

    bot.calls.clear()
    assert await sched.broadcast("纯文本", [1]) == ["1 已发送"]
    assert bot.calls[-1][1]["message"] == "纯文本"


async def test_fire_builds_a_daily_image(tmp_path, monkeypatch):
    service = StubService(tmp_path, stronghold_report_groups=[1])
    bot = StubBot()
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": bot})
    seen = {}

    async def fake_image(kind, context):
        seen["kind"] = kind
        seen["date"] = context.get("date")
        return b"\xff\xd8\xff\xe0jpg"

    monkeypatch.setattr(scheduler_mod, "build_image", fake_image)
    sched = DailyReportScheduler(service, service.config)
    await sched.fire()
    assert seen["kind"] == "daily" and seen["date"] == "2026-10-15"


def test_weekly_fires_sunday_one_minute_after_daily(tmp_path):
    service = StubService(tmp_path, stronghold_report_time="23:00", stronghold_report_tz="Asia/Shanghai")
    sched = WeeklyReportScheduler(service, service.config)

    # 2026-10-17 周六 → 次日（周日）23:01
    assert sched.next_fire_at(datetime(2026, 10, 17, 12, 0, tzinfo=TZ)) == datetime(2026, 10, 18, 23, 1, tzinfo=TZ)
    # 周日 23:00 还没到 23:01 → 还是今天
    assert sched.next_fire_at(datetime(2026, 10, 18, 23, 0, tzinfo=TZ)) == datetime(2026, 10, 18, 23, 1, tzinfo=TZ)
    # 周日 23:30 已过 → 下周日
    assert sched.next_fire_at(datetime(2026, 10, 18, 23, 30, tzinfo=TZ)) == datetime(2026, 10, 25, 23, 1, tzinfo=TZ)


def test_monthly_fires_last_day_two_minutes_after_daily(tmp_path):
    service = StubService(tmp_path, stronghold_report_time="23:00", stronghold_report_tz="Asia/Shanghai")
    sched = MonthlyReportScheduler(service, service.config)

    assert sched.next_fire_at(datetime(2026, 10, 5, 12, 0, tzinfo=TZ)) == datetime(2026, 10, 31, 23, 2, tzinfo=TZ)
    assert sched.next_fire_at(datetime(2026, 10, 31, 23, 30, tzinfo=TZ)) == datetime(2026, 11, 30, 23, 2, tzinfo=TZ)
    assert sched.next_fire_at(datetime(2028, 2, 1, 0, 0, tzinfo=TZ)) == datetime(2028, 2, 29, 23, 2, tzinfo=TZ)


def test_period_times_follow_a_changed_daily_time(tmp_path):
    service = StubService(tmp_path, stronghold_report_time="09:59", stronghold_report_tz="Asia/Shanghai")
    assert WeeklyReportScheduler(service, service.config).fire_time() == (10, 0)
    assert MonthlyReportScheduler(service, service.config).fire_time() == (10, 1)


async def test_weekly_fire_aggregates_seven_days(tmp_path, monkeypatch):
    service = StubService(tmp_path, stronghold_report_groups=[111])
    bot = StubBot()
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": bot})
    sched = WeeklyReportScheduler(service, service.config)

    results = await sched.fire()
    assert service.daily_calls == 7, "周报要取 7 天"
    assert results and "111 已发送" in results[0]
    assert "周报" in bot.sent[0][1]


async def test_period_fire_without_groups_skips(tmp_path, monkeypatch):
    service = StubService(tmp_path, stronghold_report_groups=[])
    monkeypatch.setattr(scheduler_mod, "get_bots", lambda: {"1": StubBot()})
    sched = MonthlyReportScheduler(service, service.config)
    assert await sched.fire() == []
    assert service.daily_calls == 0
