"""把 Bridge 数据渲染成 QQ 里好读的纯文本。

这里只有纯函数（无 IO、无 nonebot），所以可以直接单测；命令与日报都复用同一套渲染。
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Dict, List, Optional

from .bridge import (
    E_BAD_RESPONSE,
    E_HTTP_ERROR,
    E_NOT_IMPLEMENTED,
    E_RATE_LIMITED,
    E_TIMEOUT,
    E_UNAUTHORIZED,
    E_UNREACHABLE,
    BridgeError,
)
from .models import (
    ALL_TITLE_NAMES,
    TITLE_NAMES,
    TITLE_NOTES,
    TITLE_ORDER,
    DailyMatch,
    DailyReport,
    DailySummary,
    PlayerProfile,
    PlayerStats,
    ServerStatus,
    TitleBoard,
)

SEP = "━━━━━━━━━━━━━━"

#: 服务端错误码 → 给玩家看的中文提示。
ERROR_HINTS: Dict[str, str] = {
    E_UNREACHABLE: "连不上卫戍协议服务端（检查 stronghold_api_base 与服务端是否在跑）",
    E_TIMEOUT: "服务端响应超时，稍后再试",
    E_UNAUTHORIZED: "机器人令牌无效（服务端 BOT_TOKEN 与插件 stronghold_api_token 要一致）",
    E_NOT_IMPLEMENTED: "这台卫戍服还没实现该接入能力（服务端请参考 docs/BRIDGE-API.md）",
    "not_found": "服务端没有这个接口（接入版本可能不匹配，见 docs/BRIDGE-API.md）",
    E_RATE_LIMITED: "请求太频繁了，等一下再试",
    E_BAD_RESPONSE: "服务端返回的数据看不懂（可能是版本不匹配）",
    E_HTTP_ERROR: "服务端拒绝了这次请求",
    "service_unavailable": "服务端暂时不可用（可能在维护或重启）",
    "method_not_allowed": "服务端不接受该请求方法（接入实现有误）",
    "accounts_disabled": "这台服务器没有开启账号系统，无法用邮箱绑定",
    "smtp_failed": "服务器发不出验证码（SMTP 未配好），请找服主",
    "account_not_found": "这个邮箱没有注册过游戏账号",
    "email_missing": "请带上游戏账号的邮箱，例如 /绑定 1234@qq.com",
    "code_invalid": "验证码不对，再核对一下",
    "code_expired": "验证码已过期或错误次数过多，请重新 /绑定",
    "request_not_found": "没有待验证的绑定请求，请重新 /绑定 发码",
    "too_many_requests": "发码太频繁了，过一会儿再试",
    "bad_request": "请求参数不对（机器人侧的 bug，请反馈）",
    "player_not_found": "没找到这个玩家：服务端上没有这个账号或昵称",
    "unsupported": "服务端不支持这个操作",
}


def describe_error(err: Exception) -> str:
    """把异常变成一句给玩家看的话。"""
    if isinstance(err, BridgeError):
        hint = ERROR_HINTS.get(err.code)
        if hint:
            extra = f"（{err.message}）" if err.message and err.message not in hint else ""
            return f"❌ {hint}{extra}"
        return f"❌ {err.message or err.code}"
    return f"❌ 出错了：{err}"


# ---- 通用格式化 ---------------------------------------------------------------------------------


def fmt_time(ms: int, tz: Optional[tzinfo] = None, fmt: str = "%m-%d %H:%M") -> str:
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000, tz).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return "—"


def fmt_duration(ms: int) -> str:
    """毫秒 → ``12 分 34 秒`` / ``48 秒``。"""
    if ms <= 0:
        return "—"
    total = int(ms / 1000)
    minutes, seconds = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


def fmt_int(n: int) -> str:
    return f"{n:,}"


def fmt_win_rate(stats: PlayerStats) -> str:
    if stats.matches <= 0:
        return "—"
    rate = stats.win_rate
    if rate <= 0:
        rate = stats.victories / stats.matches
    return f"{rate * 100:.1f}%"


def fmt_titles(titles: Dict[str, int]) -> str:
    """称号计数 → ``卫戍之星 ×2 · 不朽盟约 ×1``。

    ``titles`` 来自服务端，可能包含不进榜的官方称号（``comment_2`` 不朽盟约 / ``comment_5`` 万事俱备），
    所以名字查 `ALL_TITLE_NAMES`；真不认识的 id 才退回原样显示。
    """
    parts: List[str] = []
    for key in TITLE_ORDER:
        n = titles.get(key, 0)
        if n > 0:
            parts.append(f"{ALL_TITLE_NAMES.get(key, key)} ×{n}")
    for key, n in sorted(titles.items()):
        if key not in TITLE_ORDER and n > 0:
            parts.append(f"{ALL_TITLE_NAMES.get(key, key)} ×{n}")
    return " · ".join(parts)


def stats_lines(label: str, stats: PlayerStats) -> List[str]:
    """一段「总量 + 胜率 + 消耗 + 漏怪 + 最远」的紧凑统计。"""
    if stats.matches <= 0 and not stats.gold_spent and not stats.leaks:
        return [f"{label}：暂无记录"]
    lines = [
        f"{label}：{stats.matches} 局 · {stats.victories} 胜 · 胜率 {fmt_win_rate(stats)}",
        f"　　消耗资金 {fmt_int(stats.gold_spent)} · 漏怪 {stats.leaks} · 最远 {stats.best_rounds} 回合",
    ]
    titles = fmt_titles(stats.titles)
    if titles:
        lines.append(f"　　称号 {titles}")
    return lines


# ---- 在线 ---------------------------------------------------------------------------------------


def render_online(status: ServerStatus, limit: int = 20, tz: Optional[tzinfo] = None) -> str:
    """``/在线`` 的正文：在线玩家名单。"""
    counts = status.counts
    lines = [
        f"🛡 卫戍协议 · 在线 {counts.online} 人",
        f"对局中 {counts.in_match} · 组队中 {counts.waiting} · 大厅 {counts.in_hall} · 观战 {counts.spectators}",
        SEP,
    ]
    if status.maintenance.active:
        reason = f"（{status.maintenance.reason}）" if status.maintenance.reason else ""
        lines.insert(1, f"⚠️ 服务器{status.maintenance.label}{reason}")

    players = status.players
    if not players:
        lines.append("现在没有博士在线哦 (｡•́︿•̀｡)")
        return "\n".join(lines)

    shown = players if limit <= 0 else players[:limit]
    for i, p in enumerate(shown, 1):
        bits = [p.state_label]
        if p.spectating:
            bits[0] = "观战中"
        if p.room_code:
            room = f"同盟 {p.room_code}"
            if p.is_host:
                room += "（房主）"
            bits.append(room)
        if p.difficulty:
            bits.append(p.difficulty)
        if p.joined_at:
            bits.append(f"{fmt_time(p.joined_at, tz, '%H:%M')} 起")
        lines.append(f"{i}. {p.name} · " + " · ".join(bits))

    if len(players) > len(shown):
        lines.append(f"…… 还有 {len(players) - len(shown)} 人（共 {len(players)} 人）")
    return "\n".join(lines)


def render_matches(status: ServerStatus, limit: int = 10, tz: Optional[tzinfo] = None) -> str:
    """``/对局`` 的正文：正在进行的对局。"""
    counts = status.counts
    lines = [
        f"⚔ 在线对局 {counts.matches} 局 · 房间 {counts.rooms} 个 · 观战 {counts.spectators} 人",
        SEP,
    ]
    matches = status.matches
    if not matches:
        lines.append("现在没有进行中的对局，来一局？ φ(≧ω≦*)♪")
        return "\n".join(lines)

    shown = matches if limit <= 0 else matches[:limit]
    for m in shown:
        where = m.room_code or "—"
        head = f"· {where} · {m.mode or '?'} {m.difficulty or '?'}"
        if m.boss_round:
            head += f" · 第 {m.round}/{m.boss_round} 回合"
        elif m.round:
            head += f" · 第 {m.round} 回合"
        lines.append(head)
        humans = [p for p in m.players if not p.is_bot]
        bots = [p for p in m.players if p.is_bot]
        if humans:
            who = " · ".join(f"{p.name}{'' if p.alive else '（已淘汰）'}" for p in humans)
            lines.append(f"　博士：{who}")
        if bots:
            lines.append(f"　AI：{len(bots)} 名")
        if m.started_at:
            lines.append(f"　开始于 {fmt_time(m.started_at, tz, '%H:%M')}")

    if len(matches) > len(shown):
        lines.append(f"…… 还有 {len(matches) - len(shown)} 局")
    return "\n".join(lines)


# ---- 战绩 ---------------------------------------------------------------------------------------


def _match_line(m: DailyMatch, tz: Optional[tzinfo]) -> str:
    flag = "胜" if m.victory else "负"
    parts = [fmt_time(m.at, tz), m.mode_label, m.difficulty_label, flag, f"{m.rounds_passed} 回合"]
    if m.duration_ms:
        parts.append(fmt_duration(m.duration_ms))
    return " · ".join(parts)


def render_profile(profile: PlayerProfile, limit: int = 5, tz: Optional[tzinfo] = None) -> str:
    """``/战绩`` 的正文。"""
    name = profile.name or profile.player_id or "未知博士"
    lines = [f"🎖 {name} 的战绩"]
    if profile.player_id:
        lines.append(f"账号 {profile.player_id} · 绑定 {'已绑定' if profile.bound else '未绑定（按昵称查询）'}")
    lines.append(SEP)
    lines.extend(stats_lines("今日", profile.today))
    lines.extend(stats_lines("累计", profile.total))
    if profile.recent:
        lines.append(SEP)
        lines.append("最近对局：")
        shown = profile.recent if limit <= 0 else profile.recent[:limit]
        for m in shown:
            me = next((p for p in m.players if p.player_id == profile.player_id), None)
            if me is None and len(m.players) == 1:
                me = m.players[0]
            title = ""
            if me and me.title_name:
                title = f" · {me.title_name}"
            gold = f" · 消耗 {fmt_int(me.gold_spent)}" if me else ""
            leaks = f" · 漏怪 {me.leaks}" if me and me.leaks else ""
            lines.append(f"· {_match_line(m, tz)}{title}{gold}{leaks}")
    return "\n".join(lines)


# ---- 日报 ---------------------------------------------------------------------------------------


def _board_lines(board: Optional[TitleBoard], max_holders: int = 1, title_id: str = "") -> List[str]:
    """一个称号榜的正文行（含「今日无人获得」）。服务端没给 name/note 时用内置常量兜底。"""
    key = title_id or (board.id if board else "")
    name = (board.display_name if board else "") or TITLE_NAMES.get(key, "未知称号")
    note = (board.display_note if board else "") or TITLE_NOTES.get(key, "")
    head = f"🏆 {name}" + (f"（{note}）" if note else "")
    holders = board.holders if board else []
    if not holders:
        return [head, "　今日无人获得"]

    shown = holders if max_holders <= 0 else holders[:max_holders]
    parts: List[str] = []
    for h in shown:
        if (board and board.kind == "leaks") or h.leaks > 0:
            piece = f"{h.name} {h.leaks} 只"
            if h.best:
                piece += f"（单局最多 {h.best}）"
        else:
            piece = f"{h.name} ×{h.count}"
            if h.best_rounds:
                piece += f"（最远 {h.best_rounds} 回合）"
        parts.append(piece)
    lines = [head, "　" + " · ".join(parts)]
    # 只列第一时不再报「还有 N 人」，一行就干净了
    if max_holders != 1 and len(holders) > len(shown):
        lines.append(f"　…… 还有 {len(holders) - len(shown)} 人")
    return lines


def render_daily(
    report: DailyReport, *, max_titles: int = 1, tz: Optional[tzinfo] = None, title: str = "今日战报"
) -> str:
    """``/日报`` 与定时推送共用的正文。"""
    date_text = report.date or fmt_time(report.server_now, tz, "%Y-%m-%d")
    s = report.summary
    lines = [f"🏰 {title} · {date_text}", SEP, "📊 今日概览"]

    if s.matches <= 0 and s.finished <= 0:
        lines.append("今天还没有对局记录呢 (｡•́︿•̀｡)")
    else:
        lines.append(f"对局 {s.matches or s.finished} 局 · 胜 {s.victories} / 负 {s.defeats} · 胜率 {_rate_text(s)}")
        detail = [f"参战博士 {s.players} 人"]
        if s.leaks:
            detail.append(f"漏怪 {s.leaks} 只")
        detail.append(f"消耗资金 {fmt_int(s.gold_spent)}")
        lines.append(" · ".join(detail))
        extra: List[str] = []
        if s.rounds:
            extra.append(f"累计 {s.rounds} 回合")
        if s.duration_ms:
            extra.append(f"总时长 {fmt_duration(s.duration_ms)}")
        if s.training_matches:
            extra.append(f"另有训练局 {s.training_matches} 局（不计入胜率）")
        if extra:
            lines.append(" · ".join(extra))

    lines.append(SEP)
    for title_id in TITLE_ORDER:
        lines.extend(_board_lines(report.board(title_id), max_titles, title_id))

    others = [key for key, board in sorted(report.titles.items()) if key not in TITLE_ORDER and board.holders]
    for key in others:
        lines.extend(_board_lines(report.titles[key], max_titles, key))
    return "\n".join(lines)


def _rate_text(summary: DailySummary) -> str:
    matches = summary.matches or summary.finished
    if matches <= 0:
        return "—"
    rate = summary.win_rate if summary.win_rate > 0 else summary.victories / matches
    return f"{rate * 100:.1f}%"
