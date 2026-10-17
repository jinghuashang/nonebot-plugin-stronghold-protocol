"""区间战报：把若干天的 ``/daily`` 聚合成一份（N 天 / 周 / 月）。

聚合口径跟服务端一致：

- 数字项直接相加（对局、胜负、消耗资金、漏怪、回合、时长、训练局、人次）；
- 胜率 = 胜负重新相除，不是把每天的胜率平均；
- 参战博士按 ``playerId`` 去重（单日只有一天时用服务端给的准确人数）；
- 称号榜：次数相加，漏怪榜按漏怪相加；``best`` / ``best_rounds`` 取区间最大值；
  排序跟服务端一致（称号按次数、漏怪按漏怪，都降序）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Awaitable, Callable, Dict, List, Optional

from nonebot import logger

from .bridge import BridgeError
from .models import DailyReport, DailySummary, TitleBoard, TitleHolder

#: ``async (day) -> DailyReport``，一般是 ``StrongholdService.daily``。
Fetch = Callable[[date], Awaitable[DailyReport]]


def date_range_text(start: date, end: date) -> str:
    """区间文案：同一天就是日期；同月 ``10-11 ~ 10-17``；跨月给完整日期。"""
    if start == end:
        return end.isoformat()
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.strftime('%m-%d')} ~ {end.strftime('%m-%d')}"
    return f"{start.isoformat()} ~ {end.isoformat()}"


async def period_report(fetch: Fetch, days: int, end: Optional[date] = None) -> DailyReport:
    """聚合 ``[end-days+1, end]``（含今天）的日报。

    单天取数失败只跳过那天空档并告警；全都失败才把异常抛给调用方。
    """
    end = end or date.today()
    days = max(1, int(days))
    start = end - timedelta(days=days - 1)

    reports: List[DailyReport] = []
    last_error: Optional[Exception] = None
    for offset in range(days):
        day = start + timedelta(days=offset)
        try:
            reports.append(await fetch(day))
        except BridgeError as e:
            last_error = e
            logger.warning(f"[stronghold] 区间战报：{day} 取数失败，跳过：{e}")
    if not reports and last_error is not None:
        raise last_error

    return DailyReport(
        ok=True,
        date=date_range_text(start, end),
        summary=_merge_summary(reports),
        titles=_merge_titles(reports),
    )


def _merge_summary(reports: List[DailyReport]) -> DailySummary:
    def total(attr: str) -> int:
        return sum(int(getattr(r.summary, attr) or 0) for r in reports)

    matches = total("matches")
    victories = total("victories")
    distinct = {p.player_id for r in reports for p in r.players if p.player_id}
    players = len(distinct)
    if len(reports) == 1 and reports[0].summary.players:
        players = reports[0].summary.players  # 单日时服务端给的就是准数
    return DailySummary(
        matches=matches,
        finished=total("finished"),
        victories=victories,
        defeats=total("defeats"),
        win_rate=(victories / matches) if matches else 0.0,
        players=players,
        humans=total("humans"),
        gold_spent=total("gold_spent"),
        leaks=total("leaks"),
        rounds=total("rounds"),
        duration_ms=total("duration_ms"),
        training_matches=total("training_matches"),
    )


def _merge_titles(reports: List[DailyReport]) -> Dict[str, TitleBoard]:
    merged: Dict[str, TitleBoard] = {}
    rows: Dict[str, Dict[str, TitleHolder]] = {}

    for report in reports:
        for key, board in report.titles.items():
            target = merged.get(key)
            if target is None:
                target = TitleBoard(
                    id=board.id or key,
                    name=board.name,
                    note=board.note,
                    kind=board.kind,
                )
                merged[key] = target
                rows[key] = {}
            target.name = target.name or board.name
            target.note = target.note or board.note
            if board.kind == "leaks":
                target.kind = "leaks"

            for holder in board.holders:
                ident = holder.player_id or f"name:{holder.name}"
                row = rows[key].get(ident)
                if row is None:
                    row = TitleHolder(player_id=holder.player_id, name=holder.name)
                    rows[key][ident] = row
                row.count += holder.count
                row.victories += holder.victories
                row.leaks += holder.leaks
                row.matches += holder.matches
                row.best = max(row.best, holder.best)
                row.best_rounds = max(row.best_rounds, holder.best_rounds)
                row.last_at = max(row.last_at, holder.last_at)
                if holder.name:
                    row.name = holder.name

    for key, board in merged.items():
        holders = list(rows[key].values())
        if board.kind == "leaks":
            holders.sort(key=lambda h: (h.leaks, h.best, h.name), reverse=True)
        else:
            holders.sort(key=lambda h: (h.count, h.best_rounds, h.name), reverse=True)
        board.holders = holders
        board.total = len(holders)  # 区间里的不同获奖者人数
    return merged
