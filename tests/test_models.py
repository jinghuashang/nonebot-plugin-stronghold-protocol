"""模型解析：服务端可以少给字段、给错类型，客户端必须不炸。"""

from __future__ import annotations

from nonebot_plugin_stronghold_protocol.models import (
    TITLE_NAMES,
    BindChallenge,
    BindResult,
    DailyReport,
    PlayerProfile,
    PlayerStats,
    ServerMeta,
    ServerStatus,
    TitleBoard,
)


def test_empty_payloads_get_defaults():
    status = ServerStatus.from_json({})
    assert status.ok is True
    assert status.counts.online == 0
    assert status.players == []
    assert status.maintenance.mode == "off"
    assert status.maintenance.active is False

    report = DailyReport.from_json({})
    assert report.summary.matches == 0
    assert report.titles == {}
    assert report.board("comment_1") is None

    meta = ServerMeta.from_json({})
    assert meta.capabilities == []
    assert meta.has("status") is False


def test_bad_types_are_ignored():
    stats = PlayerStats.from_json({
        "matches": "12", "victories": None, "winRate": "x", "goldSpent": -3.7,
        "leaks": True, "bestRounds": 15, "titles": {"comment_1": 2, "junk": "no"},
    })
    assert stats.matches == 0          # 字符串不当数字
    assert stats.victories == 0
    assert stats.win_rate == 0.0
    assert stats.gold_spent == -3      # 服务端给了就信（渲染时才做业务判断）
    assert stats.leaks == 0            # bool 不算数字
    assert stats.best_rounds == 15
    assert stats.titles == {"comment_1": 2}


def test_partial_status():
    status = ServerStatus.from_json({
        "counts": {"online": 3, "inMatch": 1},
        "players": [{"name": "阿米娅", "state": "match", "roomCode": "YFAF"}],
        "maintenance": {"mode": "maintenance", "reason": "BUG修复"},
    })
    assert status.counts.online == 3
    assert status.counts.rooms == 0
    player = status.players[0]
    assert player.name == "阿米娅"
    assert player.account is None
    assert player.state_label == "对局中"
    assert player.is_host is False
    assert status.maintenance.active is True
    assert status.maintenance.label == "维护中"


def test_title_board_name_falls_back_to_constants():
    board = TitleBoard.from_json({"id": "worst", "holders": [{"name": "玫兰莎", "leaks": 12}]})
    assert board.display_name == "卫戍之区" == TITLE_NAMES["worst"]
    assert board.display_note == "漏怪最多"
    assert board.kind == "leaks"
    assert board.holders[0].leaks == 12
    assert board.total == 1          # total 缺失时用行数兜底

    named = TitleBoard.from_json({"id": "comment_1", "name": "卫戍之星", "total": 5, "holders": []})
    assert named.display_name == "卫戍之星"
    assert named.total == 5


def test_daily_titles_keyed_by_map():
    report = DailyReport.from_json({
        "date": "2026-10-15",
        "titles": {"comment_1": {"holders": [{"playerId": "a_1", "name": "阿米娅", "count": 2}]}},
        "players": [{"playerId": "a_1", "name": "阿米娅", "matches": 3, "victories": 2, "goldSpent": 100}],
        "matches": [{"id": "YFAF-1", "roomCode": "YFAF", "difficulty": "HARD", "victory": True}],
    })
    assert report.board("comment_1").holders[0].name == "阿米娅"
    assert report.board("comment_1").id == "comment_1"
    assert report.players[0].stats.matches == 3      # 扁平写法也能解析
    assert report.matches[0].difficulty_label == "险境"
    assert report.matches[0].id == "YFAF-1"


def test_player_profile_shapes():
    nested = PlayerProfile.from_json({
        "player": {"playerId": "a_1", "name": "阿米娅", "bound": True},
        "total": {"matches": 42, "victories": 30},
        "today": {"matches": 1},
    })
    assert nested.player_id == "a_1"
    assert nested.name == "阿米娅"
    assert nested.bound is True
    assert nested.total.matches == 42
    assert nested.today.matches == 1

    flat = PlayerProfile.from_json({"playerId": "a_2", "name": "新手"})
    assert flat.player_id == "a_2"
    assert flat.bound is False
    assert flat.total.matches == 0


def test_bind_payloads():
    challenge = BindChallenge.from_json(
        {"requestId": "b_1", "maskedEmail": "3****@qq.com", "expiresAt": 1_700_000_000_000}
    )
    assert challenge.request_id == "b_1"
    assert challenge.expires_at == 1_700_000_000_000
    result = BindResult.from_json({"playerId": "a_1", "name": "凯尔希", "email": "3****@qq.com"})
    assert result.player_id == "a_1"
    assert result.bound_at == 0
