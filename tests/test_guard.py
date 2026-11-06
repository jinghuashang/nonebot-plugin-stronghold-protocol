"""群守护：入群验证（算术题/超时踢人）与群管命令（白名单 + 权限）。"""

from __future__ import annotations

import pytest
from test_commands import (  # noqa: F401 - plugin_service 是夹具，导入即注册
    GROUP_ID,
    dispatch,
    dispatch_notice,
    group_event,
    join_notice,
    make_bot,
    plugin_service,
)

from nonebot_plugin_stronghold_protocol import guard


@pytest.fixture(autouse=True)
def _clean_pending():
    guard.clear_pending()
    guard.clear_probes()
    yield
    guard.clear_pending()
    guard.clear_probes()


@pytest.fixture
def guarded(plugin_service):  # noqa: F811 - 参数即导入的夹具
    """白名单开在本测试群上。"""
    plugin_service.config.stronghold_manage_groups = [GROUP_ID]
    plugin_service.config.stronghold_verify_enabled = True
    plugin_service.config.stronghold_verify_timeout = 300
    plugin_service.config.stronghold_verify_mute = True
    return plugin_service


def calls(bot, api: str):
    return [data for name, data in bot.calls if name == api]


async def test_join_sends_math_question_and_answer_passes(guarded):
    bot = make_bot()
    await dispatch_notice(bot, join_notice(user_id=20001))

    assert (GROUP_ID, 20001) in guard._pending
    sent = bot.texts[-1]
    assert "进群验证" in sent and "？" in sent
    assert calls(bot, "set_group_ban")[0]["duration"] == 300, "验证期间先禁言"

    answer = guard._pending[(GROUP_ID, 20001)].answer
    await dispatch(bot, group_event(str(answer), user_id=20001))

    assert (GROUP_ID, 20001) not in guard._pending
    assert any(c["duration"] == 0 for c in calls(bot, "set_group_ban")), "答对要解禁"
    assert any(c["user_id"] == 20001 for c in calls(bot, "set_group_ban"))
    assert "验证通过" in bot.texts[-1]
    assert not calls(bot, "set_group_kick")


async def test_join_outside_whitelist_is_ignored(guarded):
    guarded.config.stronghold_manage_groups = []
    bot = make_bot()
    await dispatch_notice(bot, join_notice(user_id=20002))
    assert (GROUP_ID, 20002) not in guard._pending
    assert bot.calls == []


async def test_three_wrong_answers_kick(guarded):
    bot = make_bot()
    await dispatch_notice(bot, join_notice(user_id=20003))
    challenge = guard._pending[(GROUP_ID, 20003)]
    wrong = challenge.answer + 1

    for _ in range(2):
        await dispatch(bot, group_event(str(wrong), user_id=20003))
        assert (GROUP_ID, 20003) in guard._pending, "前两次答错不踢"

    await dispatch(bot, group_event(str(wrong), user_id=20003))
    assert (GROUP_ID, 20003) not in guard._pending
    kicked = calls(bot, "set_group_kick")
    assert kicked and kicked[0]["user_id"] == 20003 and kicked[0]["reject_add_request"] is False


async def test_timeout_kick(guarded):
    bot = make_bot()
    guard._pending[(GROUP_ID, 20004)] = guard.Challenge(answer=3, expires_at=0, muted=True)
    await guard._timeout_kick(bot, GROUP_ID, 20004, timeout=0)
    assert (GROUP_ID, 20004) not in guard._pending
    assert calls(bot, "set_group_kick")[0]["user_id"] == 20004
    assert any(c["duration"] == 0 for c in calls(bot, "set_group_ban")), "超时踢人前也要解禁"


async def test_verify_answer_does_not_leak_to_other_members(guarded):
    bot = make_bot()
    await dispatch_notice(bot, join_notice(user_id=20005))
    answer = guard._pending[(GROUP_ID, 20005)].answer
    with pytest.raises(AssertionError):
        await dispatch(bot, group_event(str(answer), user_id=20006))  # 别人替答，没有 matcher 接这条
    assert (GROUP_ID, 20005) in guard._pending


async def test_mute_needs_manager(guarded):
    bot = make_bot()
    await dispatch(bot, group_event("/禁言 ", user_id=10001, mentions=(30001,), role="member"))
    assert "只有机器人管理员或本群管理员" in bot.texts[-1]
    assert not calls(bot, "set_group_ban")


async def test_mute_by_group_admin(guarded):
    bot = make_bot()
    await dispatch(bot, group_event("/禁言 30", user_id=10001, mentions=(30002,), role="admin"))
    ban = calls(bot, "set_group_ban")
    assert ban and ban[0]["user_id"] == 30002 and ban[0]["duration"] == 30 * 60
    assert "已禁言 30002 30 分钟" in bot.texts[-1]


async def test_kick_with_reject(guarded):
    bot = make_bot()
    await dispatch(bot, group_event("/踢出 拉黑", user_id=10001, mentions=(30003,), role="owner"))
    kicked = calls(bot, "set_group_kick")
    assert kicked and kicked[0]["user_id"] == 30003 and kicked[0]["reject_add_request"] is True


async def test_recall_needs_reply(guarded):
    # 不带回复的「撤回」不该触发任何动作（它没有可撤回的目标）
    with pytest.raises(AssertionError):
        await dispatch(make_bot(), group_event("/撤回", user_id=10001, role="admin"))

    bot = make_bot()
    await dispatch(bot, group_event("/撤回", user_id=10001, role="admin", reply_id=555))
    assert calls(bot, "delete_msg")[0]["message_id"] == 555
    assert "已撤回" in bot.texts[-1]


async def test_recall_rejects_non_manager(guarded):
    bot = make_bot()
    await dispatch(bot, group_event("/撤回", user_id=10001, role="member", reply_id=556))
    assert "只有机器人管理员或本群管理员" in bot.texts[-1]
    assert not calls(bot, "delete_msg")


async def test_commands_ignore_groups_outside_whitelist(guarded):
    guarded.config.stronghold_manage_groups = []
    assert guard._managed_event(group_event("/禁言", group_id=GROUP_ID)) is False
    assert guard._managed(999) is False


# ---- 活人检测 -----------------------------------------------------------------------------------

def _members():
    return [
        {"user_id": 12345, "role": "admin", "join_time": 1},      # 机器人自己（admin）
        {"user_id": 10001, "role": "owner", "join_time": 1},      # 群主
        {"user_id": 30001, "role": "member", "join_time": 1},     # 会发言
        {"user_id": 30002, "role": "member", "join_time": 1},     # 不发言 → 踢
        {"user_id": 30003, "role": "member", "join_time": 1},     # 不发言 → 踢
    ]


def _patch_members(bot):
    async def call_api(api, **data):
        bot.calls.append((api, data))
        if api == "get_group_member_list":
            return _members()
        return {"message_id": 1, "status": "ok", "retcode": 0, "data": {"message_id": 1}}

    bot.call_api = call_api  # type: ignore[method-assign]


async def test_alive_check_kicks_silent_members(guarded):
    bot = make_bot()
    _patch_members(bot)
    await dispatch(bot, group_event("/活人检测 1", user_id=10001, role="admin"))

    probe = guard._probes[GROUP_ID]
    assert probe.candidates == [30001, 30002, 30003], "群主/管理员/机器人不参与"
    assert "@全体成员" in bot.texts[-1] or "活人检测" in bot.texts[-1]

    with pytest.raises(AssertionError):  # 监听器只记账、不发消息，dispatch 认为「没人接」
        await dispatch(bot, group_event("我活着", user_id=30001))
    assert 30001 in probe.speakers

    probe.deadline = 0  # 立刻结算
    await guard._finish_probe(bot, probe)

    kicked = sorted(c["user_id"] for c in calls(bot, "set_group_kick"))
    assert kicked == [30002, 30003]
    assert GROUP_ID not in guard._probes
    assert "2 人未发言已移出群聊" in bot.texts[-1]


async def test_alive_check_requires_manager(guarded):
    bot = make_bot()
    _patch_members(bot)
    await dispatch(bot, group_event("/活人检测", user_id=10001, role="member"))
    assert "只有机器人管理员或本群管理员" in bot.texts[-1]
    assert not guard._probes


async def test_alive_check_refuses_a_second_run(guarded):
    bot = make_bot()
    _patch_members(bot)
    await dispatch(bot, group_event("/活人检测 1", user_id=10001, role="admin"))
    bot2 = make_bot()
    _patch_members(bot2)
    await dispatch(bot2, group_event("/活人检测 1", user_id=10001, role="admin"))
    assert "已经有一场活人检测" in bot2.texts[-1]
