"""聊天命令：在线玩家 / 在线对局 / 战绩查询 / 账号绑定 / 日报。

命令都是适配器无关的：只用到 ``event.get_user_id()``、``event.get_message()``、
``group_id``（可选）和一个 ``send_group_msg``（只有日报推送用得到）。
"""

from __future__ import annotations

from typing import Optional

from nonebot import on_command
from nonebot.adapters import Event, Message
from nonebot.params import CommandArg

from . import cards
from .bridge import BridgeError
from .render import describe_error, fmt_time, render_daily, render_matches, render_online, render_profile
from .runtime import command_prefix, get_service, is_admin

#: 指令前缀：跟着部署的 COMMAND_START 走（很多部署里是空串，用户直接敲「在线」）。
P = command_prefix()


async def _send(matcher, event: Event, kind: str, context: dict, text: str) -> None:
    """发一条回复：优先出图（每类功能有自己的卡片版式），渲染不可用时回退文本。"""
    await cards.reply(matcher, event, kind, context, text)


async def _send_note(matcher, event: Event, text: str, *, title: str = "提示",
                     tone: str = "info") -> None:
    """短消息（绑定流程、用法、报错）走通用提示卡（图标由 tone 决定，不依赖 emoji 字体）。"""
    await cards.reply(matcher, event, "note", cards.views.note_view(title, text, tone), text)


async def _send_error(matcher, event: Event, text: str) -> None:
    await _send_note(matcher, event, text, title="出错了", tone="error")

USAGE_BIND = (
    f"用法：{P}绑定 <游戏账号邮箱>  → 验证码发到该邮箱\n"
    f"     {P}验证 <验证码>        → 完成绑定\n"
    f"     {P}解绑                 → 解除绑定"
)


def _mention_qq(event: Event) -> Optional[str]:
    """消息里 @ 的 QQ 号（忽略 @全体成员）。"""
    try:
        message = event.get_message()
    except Exception:  # noqa: BLE001 - 适配器不支持 get_message 时就当没 @
        return None
    for segment in message:
        if getattr(segment, "type", None) != "at":
            continue
        data = getattr(segment, "data", None) or {}
        qq = data.get("qq") if isinstance(data, dict) else None
        if isinstance(qq, str) and qq and qq != "all":
            return qq
        if isinstance(qq, int):
            return str(qq)
    return None


# ---- 在线玩家 -----------------------------------------------------------------------------------

online_cmd = on_command("在线", aliases={"在线玩家", "online"}, priority=5, block=True)


@online_cmd.handle()
async def handle_online(event: Event) -> None:
    service = get_service()
    try:
        status = await service.status()
    except BridgeError as e:
        await _send_error(online_cmd, event, describe_error(e))
        return
    await _send(
        online_cmd, event, "status",
        cards.views.status_view(status, limit=service.config.stronghold_online_limit, tz=service.tz),
        render_online(status, limit=service.config.stronghold_online_limit, tz=service.tz),
    )


# ---- 在线对局 -----------------------------------------------------------------------------------

matches_cmd = on_command("对局", aliases={"在线对局", "matches"}, priority=5, block=True)


@matches_cmd.handle()
async def handle_matches(event: Event) -> None:
    service = get_service()
    try:
        status = await service.status()
    except BridgeError as e:
        await _send_error(matches_cmd, event, describe_error(e))
        return
    await _send(
        matches_cmd, event, "matches",
        cards.views.matches_view(status, limit=service.config.stronghold_match_limit, tz=service.tz),
        render_matches(status, limit=service.config.stronghold_match_limit, tz=service.tz),
    )


# ---- 战绩查询 -----------------------------------------------------------------------------------

profile_cmd = on_command("战绩", aliases={"查询战绩", "profile"}, priority=5, block=True)


@profile_cmd.handle()
async def handle_profile(event: Event, arg: Message = CommandArg()) -> None:
    service = get_service()
    text = arg.extract_plain_text().strip()
    target_qq = _mention_qq(event)
    try:
        if target_qq:
            binding = service.binding(target_qq)
            if binding is None:
                await _send_note(profile_cmd, event, f"TA（{target_qq}）还没绑定游戏账号，让 TA 发 {P}绑定 <邮箱> 吧",
                                 title="战绩查询")
                return
            profile = await service.profile(player_id=binding.player_id)
        elif text:
            profile = await service.profile(name=text)
        else:
            profile = await service.profile_of(event.get_user_id())
    except BridgeError as e:
        if e.code == "bind_required":
            await _send_note(
                profile_cmd, event,
                "你还没绑定游戏账号。\n"
                f"先 {P}绑定 <游戏账号邮箱> 绑定，或者用 {P}战绩 <昵称> 直接按昵称查。",
                title="战绩查询",
            )
            return
        await _send_error(profile_cmd, event, describe_error(e))
        return
    await _send(
        profile_cmd, event, "profile",
        cards.views.profile_view(profile, limit=service.config.stronghold_profile_matches, tz=service.tz),
        render_profile(profile, limit=service.config.stronghold_profile_matches, tz=service.tz),
    )


# ---- 绑定 ---------------------------------------------------------------------------------------

bind_cmd = on_command("绑定", aliases={"绑定账号"}, priority=5, block=True)


@bind_cmd.handle()
async def handle_bind(event: Event, arg: Message = CommandArg()) -> None:
    service = get_service()
    parts = arg.extract_plain_text().split()
    qq = event.get_user_id()
    if not parts:
        await _send_note(bind_cmd, event, USAGE_BIND, title="绑定游戏账号")
        return

    email = parts[0]
    code = parts[1] if len(parts) >= 2 else ""

    try:
        if code:
            # /绑定 <邮箱> <验证码>：一步完成（要求之前已经发过码）
            result = await service.finish_bind(qq, code)
            await _send_note(bind_cmd, event, f"绑定成功：{result.name or result.player_id}（{result.player_id}）",
                             title="绑定游戏账号", tone="ok")
            return
        challenge = await service.start_bind(qq, email)
    except BridgeError as e:
        if e.code == "request_not_found":
            await _send_note(bind_cmd, event,
                             f"没有待验证的请求：请先发 {P}绑定 <邮箱> 获取验证码，再回 {P}验证 <验证码>",
                             title="绑定游戏账号")
            return
        await _send_error(bind_cmd, event, describe_error(e))
        return

    where = challenge.masked_email or email
    minutes = int(service.config.stronghold_bind_ttl_min)
    await _send_note(
        bind_cmd, event,
        f"验证码已发到 {where}。\n请在 {minutes} 分钟内回复：{P}验证 <验证码>",
        title="绑定游戏账号", tone="ok",
    )


verify_cmd = on_command("验证", aliases={"绑定验证"}, priority=5, block=True)


@verify_cmd.handle()
async def handle_verify(event: Event, arg: Message = CommandArg()) -> None:
    service = get_service()
    code = arg.extract_plain_text().strip()
    if not code:
        await _send_note(verify_cmd, event, f"用法：{P}验证 <验证码>（先用 {P}绑定 <邮箱> 获取）",
                         title="完成绑定")
        return
    try:
        result = await service.finish_bind(event.get_user_id(), code)
    except BridgeError as e:
        if e.code == "request_not_found":
            await _send_note(verify_cmd, event, f"没有待验证的请求：请先发 {P}绑定 <邮箱> 获取验证码",
                             title="完成绑定")
            return
        await _send_error(verify_cmd, event, describe_error(e))
        return
    await _send_note(verify_cmd, event, f"绑定成功：{result.name or result.player_id}（{result.player_id}）",
                     title="完成绑定", tone="ok")


unbind_cmd = on_command("解绑", aliases={"解绑账号"}, priority=5, block=True)


@unbind_cmd.handle()
async def handle_unbind(event: Event) -> None:
    service = get_service()
    binding = await service.unbind(event.get_user_id())
    if binding is None:
        await _send_note(unbind_cmd, event, "你还没有绑定过游戏账号", title="解绑")
        return
    await _send_note(unbind_cmd, event, f"已解绑：{binding.name or binding.player_id}（{binding.player_id}）",
                     title="解绑", tone="ok")


me_cmd = on_command("我的账号", aliases={"我的绑定", "whoami"}, priority=5, block=True)


@me_cmd.handle()
async def handle_me(event: Event) -> None:
    service = get_service()
    binding = service.binding(event.get_user_id())
    if binding is None:
        await _send_note(me_cmd, event, "你还没有绑定游戏账号。\n" + USAGE_BIND, title="我的账号")
        return
    lines = [
        f"昵称：{binding.name or '（服务端未提供）'}",
        f"playerId：{binding.player_id}",
        f"邮箱：{binding.email or '（未记录）'}",
    ]
    if binding.bound_at:
        lines.append(f"绑定时间：{fmt_time(binding.bound_at, service.tz, '%Y-%m-%d %H:%M')}")
    await _send_note(me_cmd, event, "\n".join(lines), title="我的账号")


# ---- 日报 ---------------------------------------------------------------------------------------

#: 「日报 开启 / 日报 关闭」：群里就能配，只有管理员能改。
REPORT_ON = {"开启", "打开", "开"}
REPORT_OFF = {"关闭", "关"}


async def _toggle_report(matcher, event: Event, enable: bool) -> None:
    """开关本群的日报定时推送（管理员）。"""
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        await _send_note(
            matcher, event,
            f"日报开关只在群里生效：到群里发「{P}日报 {'开启' if enable else '关闭'}」即可。",
            title="日报设置",
        )
        return
    if not is_admin(event.get_user_id()):
        await _send_note(
            matcher, event,
            "只有管理员能开关日报推送。\n"
            "管理员 = 机器人的 SUPERUSERS 或插件配置 stronghold_admins。",
            title="没有权限", tone="error",
        )
        return
    service = get_service()
    changed = service.set_report_group(group_id, enable, by=event.get_user_id())
    if service.report_group_enabled(group_id):
        if changed:
            text = (
                f"本群日报推送已开启：每天 {service.config.stronghold_report_time}"
                f"（{service.config.stronghold_report_tz}）推送当日日报。\n"
                f"随时手动查看：{P}日报"
            )
        else:
            text = f"本群日报推送本来就是开启的。\n手动查看：{P}日报"
        await _send_note(matcher, event, text, title="日报设置", tone="ok")
        return
    if changed:
        text = f"本群日报推送已关闭。\n手动查看不受影响：{P}日报"
    else:
        text = f"本群日报推送本来就是关闭的。\n手动查看：{P}日报"
    await _send_note(matcher, event, text, title="日报设置")


daily_cmd = on_command("日报", aliases={"今日日报", "daily"}, priority=5, block=True)


@daily_cmd.handle()
async def handle_daily(event: Event, arg: Message = CommandArg()) -> None:
    service = get_service()
    text = arg.extract_plain_text().strip()
    if text in REPORT_ON:
        await _toggle_report(daily_cmd, event, True)
        return
    if text in REPORT_OFF:
        await _toggle_report(daily_cmd, event, False)
        return
    today = service.report_date()
    day = service.parse_date(text, today)
    if text and day is None:
        await _send_note(daily_cmd, event, "日期看不懂呢，可以写 2026-10-15、10-15 或 昨天",
                         title="日报")
        return
    try:
        report = await service.daily(day)
    except BridgeError as e:
        await _send_error(daily_cmd, event, describe_error(e))
        return
    await _send(
        daily_cmd, event, "daily",
        cards.views.daily_view(report, max_titles=service.config.stronghold_report_max_titles, tz=service.tz),
        render_daily(report, max_titles=service.config.stronghold_report_max_titles, tz=service.tz),
    )
