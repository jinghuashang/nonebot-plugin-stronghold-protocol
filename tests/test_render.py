"""渲染层：QQ 里真正看到的那几行字（纯函数，最容易回归）。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot_plugin_stronghold_protocol.bridge import BridgeError
from nonebot_plugin_stronghold_protocol.models import (
    Counts,
    DailyReport,
    DailySummary,
    MatchPlayer,
    OnlineMatch,
    OnlinePlayer,
    PlayerProfile,
    PlayerStats,
    ServerStatus,
    TitleBoard,
    TitleHolder,
)
from nonebot_plugin_stronghold_protocol.render import (
    describe_error,
    fmt_duration,
    fmt_time,
    fmt_win_rate,
    render_daily,
    render_matches,
    render_online,
    render_profile,
)

TZ = ZoneInfo("Asia/Shanghai")


def test_fmt_helpers():
    assert fmt_duration(0) == "—"
    assert fmt_duration(48_000) == "48 秒"
    assert fmt_duration(754_000) == "12 分 34 秒"
    assert fmt_duration(3_900_000) == "1 小时 5 分"
    assert fmt_time(0) == "—"
    stamp = 1791811130971
    expected = datetime.fromtimestamp(stamp / 1000, TZ).strftime("%Y-%m-%d")
    assert fmt_time(stamp, TZ, "%Y-%m-%d") == expected
    assert fmt_win_rate(PlayerStats(matches=0)) == "—"
    assert fmt_win_rate(PlayerStats(matches=3, victories=2)) == "66.7%"
    assert fmt_win_rate(PlayerStats(matches=4, victories=2, win_rate=0.5)) == "50.0%"


def test_describe_error_uses_hints():
    assert "连不上" in describe_error(BridgeError("unreachable"))
    assert "令牌" in describe_error(BridgeError("unauthorized"))
    assert "BRIDGE-API" in describe_error(BridgeError("not_implemented"))
    assert "验证码" in describe_error(BridgeError("smtp_failed"))
    assert "验证码不对" in describe_error(BridgeError("code_invalid"))
    # 服务端自定义的错误码也要有话说
    assert "自定义问题" in describe_error(BridgeError("weird_code", "自定义问题"))


def test_render_online():
    status = ServerStatus(
        counts=Counts(online=2, in_match=1, waiting=1, in_hall=1, rooms=2, matches=1),
        players=[
            OnlinePlayer(player_id="a_1", account="a_1", name="阿米娅", state="match", room_code="YFAF",
                         seat=0, is_host=True, mode="coop", difficulty="HARD", joined_at=1791800000000),
            OnlinePlayer(player_id="p_1", name="游客", state="idle", spectating=False),
        ],
    )
    text = render_online(status, limit=20, tz=TZ)
    assert "在线 2 人" in text
    assert "1. 阿米娅 · 对局中 · 同盟 YFAF（房主） · HARD" in text
    assert "2. 游客 · 闲暇" in text


def test_render_online_truncates_and_flags_maintenance():
    from nonebot_plugin_stronghold_protocol.models import Maintenance

    players = [OnlinePlayer(name=f"博士{i}", state="idle") for i in range(5)]
    status = ServerStatus(counts=Counts(online=5), players=players,
                          maintenance=Maintenance(mode="maintenance", reason="BUG修复"))
    text = render_online(status, limit=2, tz=TZ)
    assert "服务器维护中（BUG修复）" in text
    assert "还有 3 人" in text
    assert "博士2" not in text


def test_render_online_when_empty():
    assert "没有博士在线" in render_online(ServerStatus(), tz=TZ)


def test_render_matches():
    status = ServerStatus(
        counts=Counts(matches=1, rooms=2, spectators=0),
        matches=[OnlineMatch(room_code="YFAF", mode="coop", difficulty="HARD", round=7, boss_round=14,
                             started_at=1791801700000,
                             players=[MatchPlayer(player_id="a_1", name="阿米娅", seat=0, alive=True, lp=3),
                                      MatchPlayer(player_id="p_bot", name="AI", seat=1, is_bot=True)])],
    )
    text = render_matches(status, limit=10, tz=TZ)
    assert "在线对局 1 局 · 房间 2 个" in text
    assert "YFAF · coop HARD · 第 7/14 回合" in text
    assert "博士：阿米娅" in text
    assert "AI：1 名" in text


def test_render_matches_when_empty():
    assert "没有进行中的对局" in render_matches(ServerStatus(), tz=TZ)


def test_render_profile():
    profile = PlayerProfile(
        player_id="a_1", name="阿米娅", bound=True,
        total=PlayerStats(matches=42, victories=30, win_rate=0.714, gold_spent=15230, leaks=18, best_rounds=15,
                          titles={"comment_1": 3, "worst": 1}),
        today=PlayerStats(matches=3, victories=2, gold_spent=1200, leaks=4, best_rounds=15),
        recent=[],
    )
    text = render_profile(profile, tz=TZ)
    assert "🎖 阿米娅 的战绩" in text
    assert "账号 a_1 · 绑定 已绑定" in text
    assert "今日：3 局 · 2 胜 · 胜率 66.7%" in text
    assert "消耗资金 1,200 · 漏怪 4 · 最远 15 回合" in text
    assert "累计：42 局 · 30 胜 · 胜率 71.4%" in text
    assert "称号 卫戍之星 ×3 · 卫戍之区 ×1" in text


def test_render_profile_shows_official_titles_that_are_not_boards():
    """服务端的称号次数里会有不进榜的官方称号（不朽盟约 / 万事俱备），别把它们显示成 comment_2。"""
    profile = PlayerProfile(
        player_id="a_1", name="陈", bound=True,
        total=PlayerStats(matches=1, victories=0, gold_spent=171, leaks=20, best_rounds=13,
                          titles={"comment_2": 1, "comment_5": 2, "comment_1": 1, "weird_id": 1}),
    )
    text = render_profile(profile, tz=TZ)
    assert "不朽盟约 ×1" in text
    assert "万事俱备 ×2" in text
    assert "卫戍之星 ×1" in text
    assert "comment_2" not in text and "comment_5" not in text
    assert "weird_id ×1" in text, "真不认识的 id 才原样显示"


def test_render_profile_without_records():
    text = render_profile(PlayerProfile(player_id="a_2", name="新人"), tz=TZ)
    assert "今日：暂无记录" in text
    assert "累计：暂无记录" in text
    assert "未绑定（按昵称查询）" in text


def _board(title_id: str, name: str, kind: str, holders) -> TitleBoard:
    return TitleBoard(id=title_id, name=name, kind=kind, total=len(holders), holders=holders)


def test_render_daily_full():
    report = DailyReport(
        date="2026-10-15", tz="Asia/Shanghai",
        summary=DailySummary(matches=12, finished=12, victories=8, defeats=4, win_rate=0.667, players=9,
                             humans=14, gold_spent=12345, leaks=37, rounds=88, duration_ms=12_345_678,
                             training_matches=2),
        titles={
            "comment_1": _board("comment_1", "卫戍之星", "title", [
                TitleHolder(player_id="a_1", name="阿米娅", count=2, victories=2, best_rounds=15, last_at=1)]),
            "comment_3": _board("comment_3", "坚若磐石", "title", []),
            "comment_6": _board("comment_6", "挥金如土", "title", [
                TitleHolder(player_id="a_2", name="德克萨斯", count=1, victories=1, best_rounds=14)]),
            "comment_4": _board("comment_4", "精英云集", "title", []),
            "worst": _board("worst", "卫戍之区", "leaks", [
                TitleHolder(player_id="a_3", name="炎熔", leaks=12, best=7, matches=3)]),
        },
    )
    text = render_daily(report, tz=TZ)
    assert "🏰 卫戍协议日报 · 2026-10-15" in text
    assert "对局 12 局 · 胜 8 / 负 4 · 胜率 66.7%" in text
    assert "参战博士 9 人 · 漏怪 37 只 · 消耗资金 12,345" in text
    assert "另有训练局 2 局（不计入胜率）" in text
    assert "🏆 卫戍之星（对敌方领袖造成伤害最高（仅胜利时））" in text
    assert "　阿米娅 ×2（最远 15 回合）" in text
    assert "🏆 坚若磐石（目标生命值损失最少）\n　今日无人获得" in text
    assert "　炎熔 12 只（单局最多 7）" in text


def test_render_daily_empty_day():
    report = DailyReport(date="2026-10-15", summary=DailySummary())
    text = render_daily(report, tz=TZ)
    assert "今天还没有对局记录呢" in text
    # 没有榜单数据时也要把五个称号位留出来，避免「今天没这条」变成「机器人坏了」
    assert text.count("今日无人获得") == 5


def test_render_daily_uses_builtin_names_when_server_sends_none():
    report = DailyReport(date="2026-10-15", summary=DailySummary(matches=1, finished=1, victories=1),
                         titles={"worst": TitleBoard(id="worst", holders=[TitleHolder(name="玫兰莎", leaks=3)])})
    text = render_daily(report, tz=TZ)
    assert "卫戍之区（漏怪最多）" in text
    assert "　玫兰莎 3 只" in text


def test_render_daily_max_titles():
    # 契约要求 holders 已经按口径降序（服务端排好），客户端只负责截断
    holders = [TitleHolder(player_id=f"a_{i}", name=f"博士{i}", count=i) for i in range(5, 0, -1)]
    report = DailyReport(date="2026-10-15", summary=DailySummary(matches=1, finished=1, victories=1),
                         titles={"comment_1": _board("comment_1", "卫戍之星", "title", holders)})
    text = render_daily(report, max_titles=2, tz=TZ)
    assert "博士5" in text and "博士4" in text
    assert "博士3" not in text
    assert "还有 3 人" in text


def test_render_daily_names_the_other_boards_even_when_the_server_sends_only_one():
    report = DailyReport(date="2026-10-15", summary=DailySummary(matches=1, finished=1, victories=1),
                         titles={"comment_1": _board("comment_1", "卫戍之星", "title",
                                                     [TitleHolder(player_id="a_1", name="阿米娅", count=1)])})
    text = render_daily(report, tz=TZ)
    assert "🏆 坚若磐石（目标生命值损失最少）" in text
    assert "🏆 卫戍之区（漏怪最多）" in text
    assert "未知称号" not in text
