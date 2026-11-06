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
    yield
    guard.clear_pending()


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
