"""卡片层：视图数据、HTML 渲染、图片消息拼装、以及「渲染失败回退文本」。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import types

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, PrivateMessageEvent

from nonebot_plugin_stronghold_protocol import cards, runtime
from nonebot_plugin_stronghold_protocol.cards import render as card_render
from nonebot_plugin_stronghold_protocol.cards import views
from nonebot_plugin_stronghold_protocol.config import Config
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
from nonebot_plugin_stronghold_protocol.service import StrongholdService
from nonebot_plugin_stronghold_protocol.storage import BindingStore

TZ = None
FAKE_JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


class Finished(Exception):
    """模仿 NoneBot 的 FinishedException（finish 之后中断）。"""


class FakeMatcher:
    def __init__(self) -> None:
        self.sent: list = []

    async def finish(self, message) -> None:
        self.sent.append(message)
        raise Finished


def make_bot() -> Bot:
    return Bot(Adapter(nonebot.get_driver()), "12345")


# ---- 视图：每类卡片各自的版式 ---------------------------------------------------------------


def test_status_view_groups_by_state():
    status = ServerStatus(
        counts=Counts(online=4, in_match=2, waiting=1),
        players=[
            OnlinePlayer(player_id="a_1", account="a_1", name="阿米娅", state="match", room_code="ZFNB",
                         is_host=True, difficulty="HARD", joined_at=1792117507000),
            OnlinePlayer(player_id="a_2", account="a_2", name="凯尔希", state="match", room_code="NPCD"),
            OnlinePlayer(player_id="p_3", name="路人", state="idle"),
            OnlinePlayer(player_id="a_4", account="a_4", name="陈", state="waiting", room_code="TSAW"),
        ],
    )
    view = views.status_view(status, limit=10, tz=TZ)
    assert view["kind"] == "status"
    assert [g["label"] for g in view["groups"]] == ["对局中", "待开局", "空闲"]
    assert [g["count"] for g in view["groups"]] == [2, 1, 1]
    first = view["groups"][0]["rows"][0]
    assert first["name"] == "阿米娅" and first["is_host"] is True and first["room"] == "ZFNB"


def test_matches_view_has_progress_and_squad():
    status = ServerStatus(
        counts=Counts(matches=1, rooms=1),
        matches=[
            OnlineMatch(
                room_code="NPCD", mode="coop", difficulty="ABYSS", round=7, boss_round=14,
                started_at=1792117507000,
                players=[
                    MatchPlayer(player_id="a_1", name="阿米娅", seat=0, alive=True, lp=3),
                    MatchPlayer(player_id="p_bot", name="AI", seat=1, is_bot=True),
                ],
            )
        ],
    )
    view = views.matches_view(status, tz=TZ)
    block = view["blocks"][0]
    assert block["progress"] == 50 and block["boss"] == 14 and block["mode"] == "同盟"
    assert [p["name"] for p in block["humans"]] == ["阿米娅"]
    assert block["bots"] == 1


def test_profile_view_has_big_numbers_and_medals():
    profile = PlayerProfile(
        player_id="a_0000000001", name="阿米娅", bound=True,
        total=PlayerStats(matches=42, victories=30, gold_spent=15230, leaks=18, best_rounds=15,
                          titles={"comment_1": 3, "comment_2": 1, "worst": 1}),
        today=PlayerStats(matches=3, victories=2, gold_spent=1200, leaks=4, best_rounds=15),
        recent=[],
    )
    view = views.profile_view(profile, tz=TZ)
    assert ("账号", "a_0000000001", "num") in view["chips"]
    assert ("状态", "已绑定", "num") in view["chips"]
    assert view["hero"] == {"num": "42", "cap": "累计对局", "sub": "胜率 71.4%"}
    assert view["total"]["matches"] == 42 and view["total"]["rate"] == "71.4%"
    assert view["total"]["gold"] == "15,230"
    names = [t["name"] for t in view["titles"]]
    assert "卫戍之星" in names and "不朽盟约" in names and "卫戍之区" in names, "非榜称号也要有中文名"


def test_daily_view_has_kpis_and_accent_boards():
    report = DailyReport(
        date="2026-10-16",
        summary=DailySummary(matches=12, finished=12, victories=8, defeats=4, win_rate=2 / 3, players=9,
                             humans=14, gold_spent=12345, leaks=37, rounds=88, training_matches=2),
        titles={
            "comment_1": TitleBoard(id="comment_1", name="卫戍之星", holders=[
                TitleHolder(player_id="a_1", name="阿米娅", count=2, best_rounds=15)]),
            "worst": TitleBoard(id="worst", name="卫戍之区", kind="leaks", holders=[
                TitleHolder(player_id="a_2", name="玫兰莎", leaks=12, best=7)]),
        },
    )
    view = views.daily_view(report, tz=TZ)
    kpis = {k["label"]: k["value"] for k in view["kpis"]}
    assert kpis["对局"] == "12" and kpis["胜率"] == "66.7" and kpis["消耗资金"] == "12,345"
    assert view["weekday"] == "周五"
    by_id = {b["id"]: b for b in view["boards"]}
    assert by_id["comment_1"]["accent"] == "gold"
    assert by_id["comment_1"]["texts"] == ["阿米娅 ×2（最远 15 回合）"]
    assert by_id["worst"]["accent"] == "red" and by_id["worst"]["texts"] == ["玫兰莎 12 只（单局最多 7）"]
    assert "另有训练局 2 局（不计入胜率）" in view["foot"]
    assert views.daily_view(DailyReport(date="2026-10-16"), tz=TZ)["empty"] is True


def test_note_view_strips_blank_lines():
    view = views.note_view("绑定", "第一行\n\n   \n第二行")
    assert view["lines"] == ["第一行", "第二行"]
    assert view["tone"] == "info" and view["kind"] == "note"


# ---- HTML 渲染（不截图）----------------------------------------------------------------------


@pytest.mark.parametrize("kind,context,marker", [
    ("status", views.status_view(ServerStatus(counts=Counts(online=1),
                                              players=[OnlinePlayer(name="阿米娅", state="idle")])), "阿米娅"),
    ("matches", views.matches_view(ServerStatus()), "没有进行中的对局"),
    ("profile", views.profile_view(PlayerProfile(player_id="a_1", name="阿米娅", bound=True)), "累计"),
    ("daily", views.daily_view(DailyReport(date="2026-10-16", summary=DailySummary())), "卫戍之星"),
    ("note", views.note_view("出错了", "连不上服务端", tone="error"), "连不上服务端"),
])
async def test_render_html_has_content(kind, context, marker):
    html = await card_render.render_html(kind, context)
    assert html is not None and marker in html
    # 设计语言：近黑墨绿底 + 薄荷主色 + 中文页脚品牌
    assert "--mint-500: #4ed8af" in html and "#0c0f0e" in html
    assert "nonebot-plugin-stronghold-protocol v" in html


async def test_render_html_unknown_template_returns_none():
    assert await card_render.render_html("不存在的卡片", {}) is None


# ---- 图片渲染与回退 ---------------------------------------------------------------------------


async def test_render_returns_none_without_htmlrender(monkeypatch):
    monkeypatch.setattr(card_render, "available", lambda: False)
    assert await card_render.render("note", views.note_view("t", "b")) is None


async def test_render_returns_none_when_browser_fails(monkeypatch):
    monkeypatch.setattr(card_render, "available", lambda: True)
    fake = types.ModuleType("nonebot_plugin_htmlrender")

    def boom(**_kwargs):
        raise RuntimeError("browser not ready")

    fake.get_new_page = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nonebot_plugin_htmlrender", fake)
    assert await card_render.render("note", views.note_view("t", "b")) is None


def test_image_message_for_event_and_bot():
    event = PrivateMessageEvent.model_validate({
        "time": 1, "self_id": 12345, "post_type": "message", "message_type": "private", "sub_type": "friend",
        "message_id": 1, "user_id": 10001, "message": [{"type": "text", "data": {"text": "hi"}}],
        "raw_message": "hi", "font": 0, "sender": {"user_id": 10001, "nickname": "t"},
    })
    for target in (event, make_bot()):
        message = card_render.image_message(target, FAKE_JPEG)
        assert message is not None and message[0].type == "image"
    assert card_render.image_message(object(), FAKE_JPEG) is None, "不认识的适配器要回退文本"


async def test_reply_prefers_image(monkeypatch):
    async def fake_build(kind, context):
        assert kind == "note"
        return FAKE_JPEG

    monkeypatch.setattr(cards, "build_image", fake_build)
    matcher = FakeMatcher()
    with pytest.raises(Finished):
        await cards.reply(matcher, make_bot(), "note", views.note_view("t", "b"), "文本回退")
    assert len(matcher.sent) == 1 and matcher.sent[0][0].type == "image"


async def test_reply_falls_back_to_text(monkeypatch):
    async def no_image(kind, context):
        return None

    monkeypatch.setattr(cards, "build_image", no_image)
    matcher = FakeMatcher()
    with pytest.raises(Finished):
        await cards.reply(matcher, make_bot(), "note", views.note_view("t", "b"), "文本回退")
    assert matcher.sent == ["文本回退"]


async def test_image_switch_disables_rendering(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp()) / "bindings.json"
    service = StrongholdService(Config(stronghold_reply_image=False), store=BindingStore(tmp))
    previous = runtime.get_service()
    runtime.set_service(service)
    called = []

    async def spy(kind, context):
        called.append(kind)
        return FAKE_JPEG

    monkeypatch.setattr(card_render, "render", spy)
    try:
        assert await cards.build_image("note", {}) is None
        assert called == [], "关掉开关就不该进渲染"
    finally:
        runtime.set_service(previous)
