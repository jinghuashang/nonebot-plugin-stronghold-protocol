"""用真实的 OneBot 事件驱动插件命令：验证 matcher 注册、命令解析、以及玩家真正看到的那句话。"""

from __future__ import annotations

import json
from urllib.parse import quote

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.exception import StopPropagation
from nonebot.matcher import matchers
from nonebot.message import check_and_run_matcher
from nonebot.rule import TrieRule

from nonebot_plugin_stronghold_protocol import runtime
from nonebot_plugin_stronghold_protocol.config import Config
from nonebot_plugin_stronghold_protocol.service import StrongholdService
from nonebot_plugin_stronghold_protocol.storage import BindingStore

GROUP_ID = 123456789


class Recorder(Bot):
    """记下机器人发出去的每条消息（不真的连 QQ）。"""

    def __init__(self, adapter: Adapter, self_id: str):
        super().__init__(adapter, self_id)
        self.calls: list = []

    async def call_api(self, api: str, **data):  # type: ignore[override]
        self.calls.append((api, data))
        return {"message_id": 1, "status": "ok", "retcode": 0, "data": {"message_id": 1}}

    @property
    def texts(self) -> list:
        out = []
        for _, data in self.calls:
            message = data.get("message")
            if isinstance(message, Message):
                out.append(message.extract_plain_text())
            elif isinstance(message, str):
                out.append(message)
        return out

    @property
    def reply(self) -> str:
        assert self.calls, "机器人什么都没发"
        return self.texts[-1]


def make_bot() -> Recorder:
    return Recorder(Adapter(nonebot.get_driver()), "12345")


def private_event(text: str, user_id: int = 10001) -> PrivateMessageEvent:
    return PrivateMessageEvent.model_validate({
        "time": 1791811511, "self_id": 12345, "post_type": "message", "message_type": "private",
        "sub_type": "friend", "message_id": 1, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}], "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": "测试博士"},
    })


def group_event(text: str, user_id: int = 10001, mentions: tuple = ()) -> GroupMessageEvent:
    # 真实输入是「/战绩 @某人」：命令文本在前，@ 在后
    segments = [{"type": "text", "data": {"text": text}}]
    segments.extend({"type": "at", "data": {"qq": str(qq)}} for qq in mentions)
    return GroupMessageEvent.model_validate({
        "time": 1791811511, "self_id": 12345, "post_type": "message", "message_type": "group",
        "sub_type": "normal", "message_id": 1, "group_id": GROUP_ID, "user_id": user_id,
        "message": segments, "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": "测试博士", "role": "member"},
    })


async def dispatch(bot: Recorder, event) -> None:
    """按框架的方式分发一次事件：解析命令前缀 → 命中谁就跑谁（规则的依赖解析也走真路径）。"""
    state: dict = {}
    TrieRule.get_value(bot, event, state)
    try:
        for group in matchers.values():
            for cls in group:
                before = len(bot.calls)
                await check_and_run_matcher(cls, bot, event, state, None, None)
                if len(bot.calls) > before or state.get("_matched"):
                    return
    except StopPropagation:
        return  # 框架里也是这么收敛的：handler 里 finish() 会抛它来打断事件传播
    raise AssertionError("没有任何 matcher 接住这条消息（命令没注册上？）")


@pytest.fixture
def plugin_service(tmp_path, bridge_server):
    """把插件的服务对象换成指向假 Bridge 的实例。"""
    previous = runtime.get_service()
    cfg = Config(
        stronghold_api_base=bridge_server.base_url,
        stronghold_api_token="t",
        stronghold_report_tz="Asia/Shanghai",
        stronghold_report_groups=[GROUP_ID],
        # 命令测试断言的是文本回退（出图的路径另有专门用例，见文件末尾）
        stronghold_reply_image=False,
    )
    service = StrongholdService(cfg, store=BindingStore(tmp_path / "bindings.json"))
    runtime.set_service(service)
    try:
        yield service
    finally:
        runtime.set_service(previous)


async def test_plugin_registers_every_command():
    ours = [
        cls
        for group in matchers.values()
        for cls in group
        if getattr(getattr(cls, "plugin", None), "id_", None) == "nonebot_plugin_stronghold_protocol"
    ]
    assert len(ours) == 8, "8 条命令都要注册上"
    loaded = [p for p in nonebot.get_loaded_plugins() if p.metadata and p.metadata.name == "卫戍协议"]
    assert loaded, "插件本身要被 NoneBot 登记为已加载"
    assert loaded[0].metadata.type == "application"


async def test_online_command(plugin_service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {
        "ok": True, "protocol": 1,
        "counts": {"online": 1, "inMatch": 1, "waiting": 0, "inHall": 0, "rooms": 1, "matches": 1},
        "players": [{"playerId": "a_1", "name": "阿米娅", "state": "match", "roomCode": "YFAF", "difficulty": "HARD",
                     "isHost": True}],
        "matches": [],
    })
    bot = make_bot()
    await dispatch(bot, private_event("/在线"))
    assert "在线 1 人" in bot.reply
    assert "阿米娅" in bot.reply
    assert bridge_server.last()["headers"]["authorization"] == "Bearer t"


async def test_matches_command_is_empty(plugin_service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": True, "protocol": 1, "counts": {"matches": 0}, "matches": []})
    bot = make_bot()
    await dispatch(bot, group_event("/对局"))
    assert "没有进行中的对局" in bot.reply


async def test_profile_without_binding_asks_to_bind(plugin_service, bridge_server):
    bot = make_bot()
    await dispatch(bot, private_event("/战绩"))
    assert "你还没绑定游戏账号" in bot.reply
    assert "/绑定" in bot.reply
    assert bridge_server.requests == [], "没绑定不该去打服务端"


async def test_profile_by_name(plugin_service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/player", {
        "ok": True, "protocol": 1, "player": {"playerId": "a_1", "name": "阿米娅", "bound": True},
        "total": {"matches": 3, "victories": 2, "goldSpent": 500, "leaks": 1, "bestRounds": 15},
        "today": {"matches": 1, "victories": 1},
    })
    bot = make_bot()
    await dispatch(bot, private_event("/战绩 阿米娅"))
    assert "🎖 阿米娅 的战绩" in bot.reply
    assert "累计：3 局 · 2 胜 · 胜率 66.7%" in bot.reply
    assert f"name={quote('阿米娅')}" in bridge_server.last()["path"]


async def test_profile_by_mention_uses_that_qq_binding(plugin_service, bridge_server):
    await plugin_service.store.bind("20002", "a_9", "玫兰莎")
    bridge_server.set("GET", "/api/bot/v1/player", {"ok": True, "player": {"playerId": "a_9", "name": "玫兰莎"}})
    bot = make_bot()
    await dispatch(bot, group_event("/战绩", mentions=(20002,)))
    assert "玫兰莎 的战绩" in bot.reply
    assert "playerId=a_9" in bridge_server.last()["path"]

    bot2 = make_bot()
    await dispatch(bot2, group_event("/战绩", mentions=(30003,)))
    assert "还没绑定游戏账号" in bot2.reply


async def test_bind_and_verify_flow(plugin_service, bridge_server):
    bridge_server.set("POST", "/api/bot/v1/bind/request",
                      {"ok": True, "requestId": "b_1", "maskedEmail": "3****@qq.com", "expiresAt": 0})
    bridge_server.set("POST", "/api/bot/v1/bind/verify",
                      {"ok": True, "playerId": "a_0000000001", "name": "凯尔希", "email": "3****@qq.com"})

    bot = make_bot()
    await dispatch(bot, private_event("/绑定 10001@qq.com"))
    assert "验证码已发到 3****@qq.com" in bot.reply
    assert "/验证" in bot.reply
    assert bridge_server.requests[-1]["body"] == {"email": "10001@qq.com", "qq": "10001"}

    bot2 = make_bot()
    await dispatch(bot2, private_event("/验证 123456"))
    assert "绑定成功" in bot2.reply
    assert "凯尔希" in bot2.reply
    assert plugin_service.binding("10001").player_id == "a_0000000001"

    # 绑好之后 /我的账号 与 /战绩 都走 playerId
    bridge_server.set("GET", "/api/bot/v1/player",
                      {"ok": True, "player": {"playerId": "a_0000000001", "name": "凯尔希"}})
    bot3 = make_bot()
    await dispatch(bot3, private_event("/我的账号"))
    assert "playerId：a_0000000001" in bot3.reply

    bot4 = make_bot()
    await dispatch(bot4, private_event("/战绩"))
    assert "playerId=a_0000000001" in bridge_server.last()["path"]

    bot5 = make_bot()
    await dispatch(bot5, private_event("/解绑"))
    assert "已解绑" in bot5.reply
    assert plugin_service.binding("10001") is None


async def test_bind_without_email_shows_usage(plugin_service, bridge_server):
    bot = make_bot()
    await dispatch(bot, private_event("/绑定"))
    assert "/绑定 <游戏账号邮箱>" in bot.reply
    assert bridge_server.requests == []


async def test_verify_without_request(plugin_service, bridge_server):
    bot = make_bot()
    await dispatch(bot, private_event("/验证 123456"))
    assert "先发 /绑定 <邮箱>" in bot.reply


async def test_wrong_code_says_so(plugin_service, bridge_server):
    bridge_server.set("POST", "/api/bot/v1/bind/request",
                      {"ok": True, "requestId": "b_1", "maskedEmail": "a****@qq.com"})
    bridge_server.set("POST", "/api/bot/v1/bind/verify", {"ok": False, "error": "code_invalid"}, status=400)
    await dispatch(make_bot(), private_event("/绑定 a@qq.com"))
    bot = make_bot()
    await dispatch(bot, private_event("/验证 000000"))
    assert "验证码不对" in bot.reply


async def test_daily_command(plugin_service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/daily", {
        "ok": True, "protocol": 1, "date": "2026-10-15", "tz": "Asia/Shanghai",
        "summary": {"matches": 12, "finished": 12, "victories": 8, "defeats": 4, "winRate": 0.667,
                    "players": 9, "goldSpent": 12345, "leaks": 37},
        "titles": {"comment_1": {"holders": [{"playerId": "a_1", "name": "阿米娅", "count": 2, "bestRounds": 15}]}},
    })
    bot = make_bot()
    await dispatch(bot, group_event("/日报"))
    assert "🏰 卫戍协议日报 · 2026-10-15" in bot.reply
    assert "消耗资金 12,345" in bot.reply
    assert "阿米娅 ×2" in bot.reply

    bot2 = make_bot()
    await dispatch(bot2, group_event("/日报 昨天"))
    assert "date=" in bridge_server.last()["path"]


async def test_daily_rejects_bad_date(plugin_service, bridge_server):
    bot = make_bot()
    await dispatch(bot, private_event("/日报 前天上午"))
    assert "日期看不懂" in bot.reply
    assert bridge_server.requests == []


async def test_server_down_is_explained(plugin_service, bridge_server, closed_port):
    plugin_service.client.base_url = f"http://127.0.0.1:{closed_port}"
    bot = make_bot()
    await dispatch(bot, private_event("/在线"))
    assert "❌" in bot.reply
    assert "连不上" in bot.reply or "超时" in bot.reply


async def test_bridge_not_implemented_points_at_the_docs(plugin_service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", raw=b"<html>404</html>", status=404, content_type="text/html")
    bot = make_bot()
    await dispatch(bot, private_event("/在线"))
    assert "还没实现该接入能力" in bot.reply
    assert "BRIDGE-API" in bot.reply


async def test_unknown_command_is_left_alone(plugin_service, bridge_server):
    bot = make_bot()
    with pytest.raises(AssertionError):
        await dispatch(bot, private_event("/这个命令不存在xyz"))


def test_command_segments_are_plain_text():
    assert Message(MessageSegment.at("10001")).extract_plain_text() == ""

# ---- 出图路径（默认就出图，渲染不可用时才回退文本）------------------------------------------


FAKE_JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


async def test_online_replies_with_image(plugin_service, bridge_server, monkeypatch):
    from nonebot_plugin_stronghold_protocol import cards
    from nonebot_plugin_stronghold_protocol.cards import render as card_render

    bridge_server.set("GET", "/api/bot/v1/status", {
        "ok": True, "protocol": 1,
        "counts": {"online": 1, "inMatch": 0, "waiting": 0, "inHall": 0, "rooms": 0, "matches": 0},
        "players": [{"playerId": "a_1", "name": "阿米娅", "state": "idle"}],
    })
    plugin_service.config.stronghold_reply_image = True
    seen = {}

    async def fake_render(kind, context):
        seen["kind"] = kind
        seen["context"] = context
        return FAKE_JPEG

    monkeypatch.setattr(card_render, "render", fake_render)
    bot = make_bot()
    await dispatch(bot, private_event("/在线"))
    api, data = bot.calls[-1]
    assert api.endswith("send_msg") or api.endswith("send_private_msg")
    assert data["message"][0].type == "image", "应当回一张图"
    assert seen["kind"] == "status" and seen["context"]["kind"] == "status"
    assert seen["context"]["groups"][0]["rows"][0]["name"] == "阿米娅"
    assert cards.image_message is not None


async def test_image_failure_falls_back_to_text(plugin_service, bridge_server, monkeypatch):
    from nonebot_plugin_stronghold_protocol.cards import render as card_render

    bridge_server.set("GET", "/api/bot/v1/status", {
        "ok": True, "protocol": 1, "counts": {"online": 1}, "players": [],
    })
    plugin_service.config.stronghold_reply_image = True

    async def broken(kind, context):
        return None

    monkeypatch.setattr(card_render, "render", broken)
    bot = make_bot()
    await dispatch(bot, private_event("/在线"))
    assert "在线 1 人" in bot.reply, "渲染失败要回退成文本，且内容不变"


async def test_bind_note_uses_note_card(plugin_service, bridge_server, monkeypatch):
    from nonebot_plugin_stronghold_protocol.cards import render as card_render

    bridge_server.set("POST", "/api/bot/v1/bind/request",
                      {"ok": True, "requestId": "b_1", "maskedEmail": "a****@example.com"})
    plugin_service.config.stronghold_reply_image = True
    seen = {}

    async def fake_render(kind, context):
        seen["kind"] = kind
        seen["lines"] = context.get("lines")
        seen["tone"] = context.get("tone")
        return FAKE_JPEG

    monkeypatch.setattr(card_render, "render", fake_render)
    bot = make_bot()
    await dispatch(bot, private_event("/绑定 someone@example.com"))
    assert seen["kind"] == "note"
    assert seen["tone"] == "ok"
    assert any("验证码已发到 a****@example.com" in line for line in seen["lines"])


# ---- 日报开关（群里就能配，仅管理员）------------------------------------------------------------

async def test_report_toggle_requires_admin(plugin_service):
    bot = make_bot()
    await dispatch(bot, group_event("/日报 开启", user_id=10001))
    assert "只有管理员" in bot.reply
    assert plugin_service.report_group_enabled(GROUP_ID) is True, "配置里的默认值不能被普通成员改"
    assert plugin_service.report_store.state(GROUP_ID) is None, "被拒绝的命令不该落盘"


async def test_report_toggle_by_admin(plugin_service):
    plugin_service.config.stronghold_admins = [10002]

    bot = make_bot()
    await dispatch(bot, group_event("/日报 关闭", user_id=10002))
    assert "已关闭" in bot.reply
    assert plugin_service.report_group_enabled(GROUP_ID) is False
    assert plugin_service.report_groups() == [], "关掉之后调度器就不该再推这个群"

    await dispatch(bot, group_event("/日报 开启", user_id=10002))
    assert "已开启" in bot.reply
    assert plugin_service.report_group_enabled(GROUP_ID) is True
    assert plugin_service.report_groups() == [GROUP_ID]

    doc = json.loads(plugin_service.report_store.path.read_text(encoding="utf-8"))
    assert doc["groups"][str(GROUP_ID)]["by"] == "10002", "谁改的要记下来"


async def test_report_toggle_in_private_chat_is_rejected(plugin_service):
    plugin_service.config.stronghold_admins = [10002]
    bot = make_bot()
    await dispatch(bot, private_event("/日报 开启", user_id=10002))
    assert "只在群里" in bot.reply
    assert plugin_service.report_store.state(10002) is None


async def test_report_toggle_is_idempotent(plugin_service):
    plugin_service.config.stronghold_admins = [10002]
    await dispatch(make_bot(), group_event("/日报 关闭", user_id=10002))
    bot = make_bot()
    await dispatch(bot, group_event("/日报 关闭", user_id=10002))
    assert "本来就是关闭的" in bot.reply
