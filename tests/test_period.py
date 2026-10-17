"""区间战报：日数据聚合（数字相加、胜率重算、称号合并、缺天跳过）。"""

from __future__ import annotations

from datetime import date

import pytest

from nonebot_plugin_stronghold_protocol.bridge import BridgeError
from nonebot_plugin_stronghold_protocol.models import (
    DailyPlayer,
    DailyReport,
    DailySummary,
    TitleBoard,
    TitleHolder,
)
from nonebot_plugin_stronghold_protocol.period import date_range_text, period_report


def make_day(day: date, *, matches: int, victories: int, gold: int, leaks: int, players=(), titles=None):
    return DailyReport(
        ok=True,
        date=day.isoformat(),
        summary=DailySummary(
            matches=matches,
            victories=victories,
            defeats=matches - victories,
            win_rate=(victories / matches) if matches else 0.0,
            players=len(players),
            gold_spent=gold,
            leaks=leaks,
            rounds=matches * 10,
            duration_ms=matches * 1000,
        ),
        titles=titles or {},
        players=[DailyPlayer(player_id=pid, name=pid) for pid in players],
    )


async def _fetch(table, day):
    return table[day]


async def test_period_sums_and_dedupes_players():
    days = {
        date(2026, 10, 15): make_day(date(2026, 10, 15), matches=2, victories=1, gold=100, leaks=5,
                                     players=["a_1", "a_2"]),
        date(2026, 10, 16): make_day(date(2026, 10, 16), matches=3, victories=3, gold=60, leaks=1,
                                     players=["a_2", "a_3"]),
        date(2026, 10, 17): make_day(date(2026, 10, 17), matches=1, victories=0, gold=40, leaks=4,
                                     players=["a_3"]),
    }

    report = await period_report(lambda day: _fetch(days, day), 3, end=date(2026, 10, 17))

    s = report.summary
    assert s.matches == 6 and s.victories == 4 and s.defeats == 2
    assert round(s.win_rate, 4) == round(4 / 6, 4), "胜率要重新相除，不能把每天的胜率平均"
    assert s.gold_spent == 200 and s.leaks == 10 and s.rounds == 60 and s.duration_ms == 6000
    assert s.players == 3, "参战博士按 playerId 去重（a_1 / a_2 / a_3）"
    assert report.date == "10-15 ~ 10-17"


async def test_period_merges_title_boards():
    def board(key, name, kind, holders):
        return TitleBoard(id=key, name=name, kind=kind, holders=holders)

    d1 = make_day(date(2026, 10, 16), matches=1, victories=1, gold=0, leaks=0, titles={
        "comment_1": board("comment_1", "卫戍之星", "title", [
            TitleHolder(player_id="a_1", name="阿米娅", count=1, best_rounds=14),
            TitleHolder(player_id="a_2", name="凯尔希", count=1, best_rounds=15),
        ]),
        "worst": board("worst", "卫戍之区", "leaks", [
            TitleHolder(player_id="a_2", name="凯尔希", leaks=30, best=20),
        ]),
    })
    d2 = make_day(date(2026, 10, 17), matches=1, victories=0, gold=0, leaks=0, titles={
        "comment_1": board("comment_1", "卫戍之星", "title", [
            TitleHolder(player_id="a_2", name="凯尔希", count=2, best_rounds=15),
        ]),
        "worst": board("worst", "卫戍之区", "leaks", [
            TitleHolder(player_id="a_2", name="凯尔希", leaks=12, best=40),
        ]),
    })
    days = {date(2026, 10, 16): d1, date(2026, 10, 17): d2}

    report = await period_report(lambda day: _fetch(days, day), 2, end=date(2026, 10, 17))

    star = report.board("comment_1")
    assert [h.name for h in star.holders] == ["凯尔希", "阿米娅"], "次数相加后重新排序"
    assert star.holders[0].count == 3 and star.holders[0].best_rounds == 15
    assert star.total == 2, "total = 区间的不同获奖者"

    worst = report.board("worst")
    assert worst.kind == "leaks"
    assert worst.holders[0].leaks == 42 and worst.holders[0].best == 40


async def test_period_skips_a_missing_day():
    calls = []

    async def fetch(day):
        calls.append(day)
        if day == date(2026, 10, 16):
            raise BridgeError("unreachable")
        return make_day(day, matches=1, victories=1, gold=10, leaks=0)

    report = await period_report(fetch, 2, end=date(2026, 10, 17))
    assert len(calls) == 2, "缺的天也要试一次"
    assert report.summary.matches == 1, "缺天按空数据跳过，不能整份失败"


async def test_period_raises_when_every_day_fails():
    async def boom(day):
        raise BridgeError("unreachable")

    with pytest.raises(BridgeError):
        await period_report(boom, 3, end=date(2026, 10, 17))


def test_date_range_text():
    assert date_range_text(date(2026, 10, 17), date(2026, 10, 17)) == "2026-10-17"
    assert date_range_text(date(2026, 10, 11), date(2026, 10, 17)) == "10-11 ~ 10-17"
    assert date_range_text(date(2026, 9, 28), date(2026, 10, 4)) == "2026-09-28 ~ 2026-10-04"
