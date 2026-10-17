"""把数据整理成卡片模板要用的形状（纯函数，方便单测）。

四类卡片各有自己的版式，不是同一套表格换标题：

| kind | 版式 | 关键元素 |
|---|---|---|
| `status` | 在线名册 | 按状态分组 + 组内计数 + 状态色标 |
| `matches` | 战况面板 | 每房一块 + **回合进度条** + 参战博士与 LP |
| `profile` | 个人档案 | 今日 / 累计大数字面板 + 称号勋章 + 最近对局 |
| `daily` | 日报海报 | 日期大字 + KPI 条 + 五个称号（各自配色） |
| `note` | 通用提示 | 一行标题 + 若干正文（绑定流程、报错、用法） |
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Any, Dict, List, Optional

from ..models import (
    ALL_TITLE_NAMES,
    TITLE_ACCENTS,
    TITLE_INFO,
    TITLE_ORDER,
    DailyReport,
    PlayerProfile,
    PlayerStats,
    ServerStatus,
    TitleBoard,
)
from ..render import fmt_duration, fmt_time, fmt_win_rate

#: 卡片画布宽度（QQ 里图片显示宽度大致如此；高度由内容决定）。
WIDTH = 750

#: 名册分组的顺序与标题（状态 → 中文）。
ROSTER_GROUPS = [
    ("match", "对局中"),
    ("waiting", "组队中"),
    ("spectating", "观战中"),
    ("hall", "大厅"),
    ("idle", "闲暇"),
]


def note_view(title: str, body: str, tone: str = "info") -> Dict[str, Any]:
    """通用提示卡片（绑定流程、报错、用法提示）。``tone``：``info`` / ``ok`` / ``error``。"""
    lines = [line for line in body.splitlines() if line.strip()]
    return {
        "kind": "note",
        "crumb": "卫戍协议 / 提示",
        "title": title,
        "tone": tone,
        "lines": lines,
    }


def status_view(status: ServerStatus, limit: int = 20, tz: Optional[tzinfo] = None) -> Dict[str, Any]:
    counts = status.counts
    chips = [("在线", str(counts.online), "num"), ("对局中", str(counts.in_match), "num")]
    if counts.waiting:
        chips.append(("组队中", str(counts.waiting), "num"))
    if counts.in_hall:
        chips.append(("大厅", str(counts.in_hall), "num"))
    if counts.spectators:
        chips.append(("观战", str(counts.spectators), "num"))

    # 上限按「每组」算：先分组再截断，否则人多的组会把其他状态整组挤掉。
    groups: List[Dict[str, Any]] = []
    hidden = 0
    for key, label in ROSTER_GROUPS:
        members = [p for p in status.players if ("spectating" if p.spectating else p.state) == key]
        if not members:
            continue
        shown = members if limit <= 0 else members[:limit]
        hidden += len(members) - len(shown)
        groups.append({
            "key": key,
            "label": label,
            "count": len(members),
            "hidden": len(members) - len(shown),
            "rows": [
                {
                    "name": p.name,
                    "is_host": p.is_host,
                    "room": p.room_code or "",
                    "difficulty": p.difficulty or "",
                    "since": fmt_time(p.joined_at, tz, "%H:%M"),
                }
                for p in shown
            ],
        })
    return {
        "kind": "status",
        "crumb": "卫戍协议 / 在线玩家",
        "title": "在线玩家",
        "hero": {
            "num": str(counts.online),
            "cap": "人在线",
            "sub": f"服 v{status.app}" if status.app else "实时在线",
        },
        "lede": {
            "title": "使用指引",
            "lines": [
                "按状态分组；「同盟 ××××」是房间码，方便约人。",
                "数据来自服务端实时状态，打开卡片时拉取。",
            ],
        },
        "chips": chips,
        "groups": groups,
        "more": hidden,
        "maintenance": status.maintenance.reason if status.maintenance.active else None,
    }


def matches_view(status: ServerStatus, limit: int = 10, tz: Optional[tzinfo] = None) -> Dict[str, Any]:
    counts = status.counts
    shown = status.matches if limit <= 0 else status.matches[:limit]
    blocks = []
    for m in shown:
        boss = m.boss_round or 14
        progress = int(max(0.0, min(1.0, m.round / boss)) * 100) if boss else 0
        humans = [
            {"name": p.name, "lp": p.lp, "alive": p.alive, "seat": p.seat}
            for p in m.players if not p.is_bot
        ]
        bots = len([p for p in m.players if p.is_bot])
        blocks.append({
            "room": m.room_code or "—",
            "mode": "独立" if m.mode == "solo" else ("同盟" if m.mode == "coop" else (m.mode or "?")),
            "difficulty": m.difficulty or "?",
            "round": m.round,
            "boss": boss,
            "progress": progress,
            "humans": humans,
            "bots": bots,
            "started": fmt_time(m.started_at, tz, "%H:%M"),
        })
    return {
        "kind": "matches",
        "crumb": "卫戍协议 / 在线对局",
        "title": "在线对局",
        "hero": {
            "num": str(counts.matches),
            "cap": "局进行中",
            "sub": f"{counts.rooms} 个房间",
        },
        "lede": {
            "title": "怎么读",
            "lines": [
                "进度条 = 当前回合 / 最终回合。",
                "LP 是博士当前生命，灰掉 = 已阵亡。",
            ],
        },
        "chips": [("对局", str(counts.matches), "num"), ("房间", str(counts.rooms), "num"),
                  ("观战", str(counts.spectators), "num")],
        "blocks": blocks,
        "more": max(0, len(status.matches) - len(shown)),
    }


def _big_rows(stats: PlayerStats) -> Dict[str, Any]:
    return {
        "matches": stats.matches,
        "victories": stats.victories,
        "rate": fmt_win_rate(stats),
        "gold": f"{stats.gold_spent:,}",
        "leaks": stats.leaks,
        "best": stats.best_rounds,
    }


def profile_view(profile: PlayerProfile, limit: int = 5, tz: Optional[tzinfo] = None) -> Dict[str, Any]:
    titles = [
        {"name": ALL_TITLE_NAMES.get(k, k), "count": n}
        for k, n in profile.total.titles.items() if n > 0
    ]
    titles.sort(key=lambda x: -x["count"])

    recent = []
    for m in (profile.recent if limit <= 0 else profile.recent[:limit]):
        me = next((p for p in m.players if p.player_id == profile.player_id), None)
        if me is None and len(m.players) == 1:
            me = m.players[0]
        recent.append({
            "when": fmt_time(m.at, tz),
            "where": f"{m.mode_label} · {m.difficulty_label}",
            "win": m.victory,
            "rounds": m.rounds_passed,
            "duration": fmt_duration(m.duration_ms),
            "title": (me.title_name if me and me.title_name else "—"),
            "gold": f"{me.gold_spent:,}" if me else "—",
            "leaks": me.leaks if me else 0,
        })
    chips = []
    if profile.player_id:
        chips.append(("账号", profile.player_id, "num"))
    chips.append(("状态", "已绑定" if profile.bound else "未绑定", "num"))
    return {
        "kind": "profile",
        "crumb": "卫戍协议 / 战绩查询",
        "title": profile.name or profile.player_id or "未知博士",
        "hero": {
            "num": str(profile.total.matches),
            "cap": "累计对局",
            "sub": f"胜率 {fmt_win_rate(profile.total)}",
        },
        "lede": {
            "title": "数据来源",
            "lines": [
                "服务端结算记录；「今日」按配置时区划分。",
                "绑定账号后，发「战绩」不填昵称就是你自己。",
            ],
        },
        "chips": chips,
        "today": _big_rows(profile.today),
        "total": _big_rows(profile.total),
        "titles": titles,
        "recent": recent,
    }


def _board_block(title_id: str, board: Optional[TitleBoard], max_holders: int) -> Dict[str, Any]:
    name = (board.display_name if board else "") or ALL_TITLE_NAMES.get(title_id, title_id)
    note = (board.display_note if board else "") or TITLE_INFO.get(title_id, ("", ""))[1]
    accent = TITLE_ACCENTS.get(title_id, "mint")
    holders = board.holders if board else []
    shown = holders if max_holders <= 0 else holders[:max_holders]
    texts = []
    for h in shown:
        if (board and board.kind == "leaks") or h.leaks > 0:
            piece = f"{h.name} {h.leaks} 只"
            if h.best:
                piece += f"（单局最多 {h.best}）"
        else:
            piece = f"{h.name} ×{h.count}"
            if h.best_rounds:
                piece += f"（最远 {h.best_rounds} 回合）"
        texts.append(piece)
    return {
        "id": title_id,
        "name": name,
        "note": note,
        "accent": accent,
        "texts": texts,
        # 只列第一时不再报「还有 N 人」
        "more": 0 if max_holders == 1 else max(0, len(holders) - len(shown)),
    }


def daily_view(report: DailyReport, max_titles: int = 1, tz: Optional[tzinfo] = None) -> Dict[str, Any]:
    s = report.summary
    matches = s.matches or s.finished
    rate = s.win_rate if s.win_rate > 0 else (s.victories / matches if matches else 0.0)
    kpis = [
        {"label": "对局", "value": str(matches), "unit": "局", "cls": "num"},
        {"label": "胜率", "value": f"{rate * 100:.1f}", "unit": "%", "cls": "num"},
        {"label": "消耗资金", "value": f"{s.gold_spent:,}", "unit": "", "cls": "gold"},
        {"label": "漏怪", "value": str(s.leaks), "unit": "只", "cls": "num"},
        {"label": "参战博士", "value": str(s.players), "unit": "人", "cls": "num"},
    ]
    boards = [_board_block(tid, report.board(tid), max_titles) for tid in TITLE_ORDER]
    for key, board in sorted(report.titles.items()):
        if key not in TITLE_ORDER and board.holders:
            boards.append(_board_block(board.id or key, board, max_titles))
    foot = []
    if matches:
        foot.append(f"胜 {s.victories} / 负 {s.defeats}")
    if s.humans:
        foot.append(f"{s.humans} 人次")
    if s.rounds:
        foot.append(f"累计 {s.rounds} 回合")
    if s.duration_ms:
        foot.append(f"总时长 {fmt_duration(s.duration_ms)}")
    if s.training_matches:
        foot.append(f"另有训练局 {s.training_matches} 局（不计入胜率）")
    date_text = report.date or fmt_time(report.server_now, tz, "%Y-%m-%d")
    weekday = ""
    try:
        from datetime import date as _date

        weekday = "周" + "一二三四五六日"[_date.fromisoformat(date_text).weekday()]
    except Exception:  # noqa: BLE001 - 日期格式奇怪就不显示星期
        weekday = ""
    return {
        "kind": "daily",
        "crumb": "卫戍协议 / 今日日报",
        "title": "今日日报",
        "hero": {
            "num": str(matches),
            "cap": "今日对局",
            "sub": f"{date_text} {weekday}".strip(),
        },
        "lede": {
            "title": "统计口径",
            "lines": [
                "五个称号 + 今日胜率 + 今日消耗资金。",
                "训练局不计入胜率；" + (
                    "每个称号只列第一人。" if max_titles == 1
                    else (f"每个称号最多列 {max_titles} 人。" if max_titles > 0 else "称号全部列出。")
                ),
            ],
        },
        "date": date_text,
        "weekday": weekday,
        "kpis": kpis,
        "boards": boards,
        "foot": foot,
        "empty": matches <= 0,
    }
