"""群聊守护：入群验证 + 群管命令（只对白名单群生效）。

- **入群验证**：新成员进群 → 出一道算术题 @ 他 → 答对通过；没在 ``stronghold_verify_timeout``
  内答对就移出群聊。**验证期间绝不禁言**（禁言了就发不出答案，等于必踢）。
- **群管命令**：``禁言 @人 [分钟]`` / ``解禁 @人`` / ``踢出 @人 [拉黑]`` / ``撤回``（回复一条消息）。
  只有白名单群（``stronghold_manage_groups``）里、且操作者是机器人管理员或群管理员/群主时可用。

为什么要有白名单：机器人通常进了很多群，这些动作（尤其踢人）必须显式授权到具体群。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from nonebot import logger, on_command, on_message, on_notice
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule

from .runtime import command_prefix, get_service, is_admin

P = command_prefix()

#: 默认禁言时长（分钟）。
DEFAULT_MUTE_MINUTES = 10
#: 禁言上限（分钟）：42 天，够用且不会被 OneBot 的参数边界坑到。
MAX_MUTE_MINUTES = 60 * 24 * 30
#: 答错多少次后直接踢（超时是兜底，不让人无限试）。
MAX_ATTEMPTS = 3


@dataclass
class Challenge:
    """一条待验证的入群请求。"""

    answer: int
    expires_at: float
    attempts: int = 0
    task: Optional[asyncio.Task] = None


#: (group_id, user_id) → 待验证的题。
_pending: Dict[Tuple[int, int], Challenge] = {}


@dataclass
class Probe:
    """一次「活人检测」：@全体成员，宽限期内没说过话的普通成员踢出。"""

    group_id: int
    started_at: float
    deadline: float
    candidates: List[int] = field(default_factory=list)
    speakers: Set[int] = field(default_factory=set)
    task: Optional[asyncio.Task] = None


#: group_id → 正在进行的检测。
_probes: Dict[int, Probe] = {}


def pending_count() -> int:
    return len(_pending)


def clear_probes() -> None:
    for probe in _probes.values():
        if probe.task and not probe.task.done():
            probe.task.cancel()
    _probes.clear()


def clear_pending() -> None:
    """测试/停机用：取消所有等待中的计时任务。"""
    for challenge in _pending.values():
        if challenge.task and not challenge.task.done():
            challenge.task.cancel()
    _pending.clear()


# ---- 配置/权限 ---------------------------------------------------------------------------------

def _config():
    return get_service().config


def _managed(group_id: object) -> bool:
    """这个群在白名单里吗（所有群管动作与入群验证都只看它）。"""
    try:
        gid = int(group_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return gid in {int(g) for g in _config().stronghold_manage_groups}


def _is_manager(event: GroupMessageEvent) -> bool:
    """机器人管理员，或这个群的管理员/群主。"""
    if is_admin(event.get_user_id()):
        return True
    role = getattr(getattr(event, "sender", None), "role", "") or ""
    return role in ("admin", "owner")


def _targets(event: GroupMessageEvent) -> list:
    """消息里被 @ 的人（忽略 @全体成员 和自己）。"""
    me = int(event.self_id)
    out = []
    for seg in event.get_message():
        if seg.type != "at":
            continue
        qq = str(seg.data.get("qq", ""))
        if not qq.isdigit():
            continue
        uid = int(qq)
        if uid != me and uid not in out:
            out.append(uid)
    return out


def _reply_id(event: GroupMessageEvent) -> Optional[int]:
    """被回复的消息 id（nonebot 的 v11 适配器没有现成的 ``event.reply``，自己从消息段里取）。"""
    for seg in event.get_message():
        if seg.type == "reply":
            try:
                return int(seg.data.get("id"))
            except (TypeError, ValueError):
                return None
    return None


def _minutes(text: str, default: int = DEFAULT_MUTE_MINUTES) -> int:
    """``禁言 @人 30`` → 30（分钟）；非法/负数退回默认值，并夹到上限。"""
    for token in re.findall(r"\d+", text or ""):
        value = int(token)
        if value > 0:
            return min(value, MAX_MUTE_MINUTES)
    return default


async def _say(bot: Bot, group_id: int, text: str) -> None:
    try:
        await bot.send_group_msg(group_id=group_id, message=text)
    except Exception as e:  # noqa: BLE001 - 发不出去只记日志
        logger.warning(f"[stronghold] 群 {group_id} 发消息失败：{type(e).__name__}: {e}")


# ---- 入群验证 ---------------------------------------------------------------------------------

def _is_join(event: Event) -> bool:
    return isinstance(event, GroupIncreaseNoticeEvent)


join_notice = on_notice(rule=Rule(_is_join), priority=5, block=False)


@join_notice.handle()
async def handle_join(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    config = _config()
    if not config.stronghold_verify_enabled or not _managed(event.group_id):
        return
    user_id = int(event.user_id)
    if user_id == int(event.self_id):
        return
    group_id = int(event.group_id)
    timeout = max(30, int(config.stronghold_verify_timeout))

    a, b = random.randint(2, 19), random.randint(2, 19)
    answer = a + b
    # 铁律：验证期间**不能禁言** —— 禁言了对方就发不出答案，必然超时被踢。
    # 防刷屏靠的是「超时/答错 3 次就移出」，以及管理员手动禁言。
    challenge = Challenge(answer=answer, expires_at=time.time() + timeout)
    _pending[(group_id, user_id)] = challenge
    challenge.task = asyncio.create_task(
        _timeout_kick(bot, group_id, user_id, timeout), name=f"stronghold-verify-{group_id}-{user_id}"
    )

    minutes = max(1, timeout // 60)
    await bot.send_group_msg(
        group_id=group_id,
        message=Message(
            [
                MessageSegment.at(user_id),
                MessageSegment.text(
                    f" 进群验证：{a} + {b} = ？\n请在 {minutes} 分钟内直接回复答案，"
                    f"超时或答错 {MAX_ATTEMPTS} 次会被移出群聊。"
                ),
            ]
        ),
    )


async def _timeout_kick(bot: Bot, group_id: int, user_id: int, timeout: int) -> None:
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        return
    challenge = _pending.pop((group_id, user_id), None)
    if challenge is None:
        return
    await _kick(bot, group_id, user_id, reason="验证超时")


async def _kick(bot: Bot, group_id: int, user_id: int, *, reason: str, reject: bool = False) -> bool:
    try:
        await bot.set_group_kick(group_id=group_id, user_id=user_id, reject_add_request=reject)
    except Exception as e:  # noqa: BLE001 - 没权限/已退群都只记日志
        logger.warning(f"[stronghold] 群 {group_id} 移出 {user_id} 失败：{type(e).__name__}: {e}")
        return False
    await _say(bot, group_id, f"⚠️ {user_id} {reason}，已移出群聊。")
    return True


def _waiting(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and (int(event.group_id), int(event.user_id)) in _pending


verify_answer = on_message(rule=Rule(_waiting), priority=1, block=True)


@verify_answer.handle()
async def handle_answer(bot: Bot, event: GroupMessageEvent) -> None:
    key = (int(event.group_id), int(event.user_id))
    challenge = _pending.get(key)
    if challenge is None:
        return
    text = event.get_message().extract_plain_text()
    numbers = [int(x) for x in re.findall(r"-?\d+", text)]
    if challenge.answer in numbers:
        _pending.pop(key, None)
        if challenge.task and not challenge.task.done():
            challenge.task.cancel()
        await _say(bot, key[0], f"✅ 验证通过，欢迎 {event.user_id} 进群。")
        return

    challenge.attempts += 1
    if challenge.attempts >= MAX_ATTEMPTS:
        _pending.pop(key, None)
        if challenge.task and not challenge.task.done():
            challenge.task.cancel()
        await _kick(bot, key[0], key[1], reason=f"答错 {MAX_ATTEMPTS} 次")
        return
    left = MAX_ATTEMPTS - challenge.attempts
    await _say(bot, key[0], f"❌ 答案不对，再试一次（还剩 {left} 次机会）。")


# ---- 群管命令 ---------------------------------------------------------------------------------

def _managed_event(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and _managed(event.group_id)


mute_cmd = on_command("禁言", aliases={"禁言成员"}, priority=5, block=True, rule=Rule(_managed_event))
unmute_cmd = on_command("解禁", aliases={"解除禁言"}, priority=5, block=True, rule=Rule(_managed_event))
kick_cmd = on_command("踢出", aliases={"踢人", "移出"}, priority=5, block=True, rule=Rule(_managed_event))

DENY = "只有机器人管理员或本群管理员能使用群管命令。"


@mute_cmd.handle()
async def handle_mute(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()) -> None:
    if not _is_manager(event):
        await _say(bot, event.group_id, DENY)
        return
    targets = _targets(event)
    if not targets:
        await _say(bot, event.group_id, f"用法：{P}禁言 @某人 [分钟]（默认 {DEFAULT_MUTE_MINUTES} 分钟）")
        return
    minutes = _minutes(arg.extract_plain_text())
    for uid in targets:
        try:
            await bot.set_group_ban(group_id=event.group_id, user_id=uid, duration=minutes * 60)
        except Exception as e:  # noqa: BLE001
            await _say(bot, event.group_id, f"禁言 {uid} 失败：{type(e).__name__}（机器人需要是管理员）")
            continue
        await _say(bot, event.group_id, f"🔇 已禁言 {uid} {minutes} 分钟。")


@unmute_cmd.handle()
async def handle_unmute(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()) -> None:
    if not _is_manager(event):
        await _say(bot, event.group_id, DENY)
        return
    targets = _targets(event)
    if not targets:
        await _say(bot, event.group_id, f"用法：{P}解禁 @某人")
        return
    for uid in targets:
        try:
            await bot.set_group_ban(group_id=event.group_id, user_id=uid, duration=0)
        except Exception as e:  # noqa: BLE001
            await _say(bot, event.group_id, f"解禁 {uid} 失败：{type(e).__name__}")
            continue
        await _say(bot, event.group_id, f"🔊 已解禁 {uid}。")


@kick_cmd.handle()
async def handle_kick(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()) -> None:
    if not _is_manager(event):
        await _say(bot, event.group_id, DENY)
        return
    targets = _targets(event)
    if not targets:
        await _say(bot, event.group_id, f"用法：{P}踢出 @某人 [拉黑]（拉黑 = 拒绝再次加群）")
        return
    reject = "拉黑" in arg.extract_plain_text()
    for uid in targets:
        ok = await _kick(bot, event.group_id, uid, reason="被管理员移出", reject=reject)
        if not ok:
            await _say(bot, event.group_id, f"移出 {uid} 失败：机器人需要是管理员，或对方已不在群里。")


#: 「撤回」必须配合「回复某条消息」用，而带回复的消息第一段是 reply 而不是文本，
#: nonebot 的 TrieRule 只认第一段文本 —— 所以这条命令不能用 on_command，改用消息规则。
RECALL_WORDS = {"撤回", "撤回消息", "撤销", "recall"}


def _recall_wanted(event: Event) -> bool:
    if not isinstance(event, GroupMessageEvent) or not _managed(event.group_id):
        return False
    if _reply_id(event) is None:
        return False
    return event.get_plaintext().strip().lstrip("!/#. ").strip() in RECALL_WORDS


recall_msg = on_message(rule=Rule(_recall_wanted), priority=5, block=True)


@recall_msg.handle()
async def handle_recall(bot: Bot, event: GroupMessageEvent) -> None:
    if not _is_manager(event):
        await _say(bot, event.group_id, DENY)
        return
    reply_id = _reply_id(event)
    if reply_id is None:
        await _say(bot, event.group_id, f"用法：回复要撤回的消息，发送「{P}撤回」")
        return
    try:
        await bot.delete_msg(message_id=reply_id)
    except Exception as e:  # noqa: BLE001
        await _say(bot, event.group_id, f"撤回失败：{type(e).__name__}（机器人需要是管理员，且消息未超时）")
        return
    await _say(bot, event.group_id, "🧹 已撤回。")


# ---- 活人检测 ---------------------------------------------------------------------------------

def _probe_active(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and int(event.group_id) in _probes


probe_listen = on_message(rule=Rule(_probe_active), priority=10, block=False)


@probe_listen.handle()
async def handle_probe_talk(event: GroupMessageEvent) -> None:
    probe = _probes.get(int(event.group_id))
    if probe is not None:
        probe.speakers.add(int(event.user_id))


async def _finish_probe(bot: Bot, probe: Probe) -> None:
    try:
        await asyncio.sleep(max(0.0, probe.deadline - time.time()))
    except asyncio.CancelledError:
        return
    if _probes.get(probe.group_id) is not probe:
        return
    _probes.pop(probe.group_id, None)

    silent = [uid for uid in probe.candidates if uid not in probe.speakers]
    kicked, failed = 0, 0
    for uid in silent:
        try:
            await bot.set_group_kick(group_id=probe.group_id, user_id=uid, reject_add_request=False)
            kicked += 1
        except Exception as e:  # noqa: BLE001 - 没权限/已退群不算失败整体
            failed += 1
            logger.warning(f"[stronghold] 活人检测：移出 {uid} 失败：{type(e).__name__}: {e}")
        await asyncio.sleep(0.4)

    text = f"✅ 活人检测结束：{len(probe.speakers)} 人发言，{kicked} 人未发言已移出群聊。"
    if failed:
        text += f"（{failed} 人移出失败，需手动处理）"
    await _say(bot, probe.group_id, text)


alive_cmd = on_command("活人检测", aliases={"全员点名", "点名"}, priority=5, block=True,
                       rule=Rule(_managed_event))


@alive_cmd.handle()
async def handle_alive_check(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()) -> None:
    if not _is_manager(event):
        await _say(bot, event.group_id, DENY)
        return
    group_id = int(event.group_id)
    if group_id in _probes:
        await _say(bot, group_id, "这个群已经有一场活人检测在进行中。")
        return
    minutes = min(60, max(1, _minutes(arg.extract_plain_text(), default=10)))

    try:
        members = await bot.get_group_member_list(group_id=group_id)
    except Exception as e:  # noqa: BLE001
        await _say(bot, group_id, f"取群成员列表失败：{type(e).__name__}")
        return

    now = time.time()
    candidates = []
    for m in members or []:
        uid = int(m.get("user_id") or 0)
        if not uid or uid == int(event.self_id):
            continue
        if str(m.get("role") or "member") != "member":
            continue  # 群主/管理员不参与
        if int(m.get("join_time") or 0) > now:
            continue
        candidates.append(uid)

    probe = Probe(group_id=group_id, started_at=now, deadline=now + minutes * 60, candidates=candidates)
    _probes[group_id] = probe
    probe.task = asyncio.create_task(_finish_probe(bot, probe), name=f"stronghold-probe-{group_id}")

    await bot.send_group_msg(
        group_id=group_id,
        message=Message([
            MessageSegment.at("all"),
            MessageSegment.text(
                f"\n【活人检测】请 {minutes} 分钟内在本群随便发一句话（表情、符号、数字都行）。"
                f"\n{minutes} 分钟内没有任何发言的成员会被移出群聊。"
            ),
        ]),
    )
