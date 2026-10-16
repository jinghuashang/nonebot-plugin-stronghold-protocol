"""菜单用的实时数据层：**不依赖 picmenu-next**，可单独测试。

只做三件事：TTL 缓存、并发去重（single-flight）、失败降级（返回 None）。
菜单渲染绝不能因为游戏服连不上而报错。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Dict, Optional

from ..models import TITLE_ACCENTS, TITLE_NAMES, TITLE_ORDER, DailyReport, ServerStatus
from ..service import StrongholdService

#: 单次取数超时（秒）：菜单是「顺路显示」，慢的宁可不显示。
DEFAULT_TIMEOUT = 2.0
#: 成功结果的缓存时长（秒）。
DEFAULT_TTL = 30.0
#: 失败结果也缓存一小会儿，避免服务端挂了之后每个请求都去打它。
DEFAULT_FAILURE_TTL = 10.0


def _first_holder_text(title_id: str, board) -> str:
    """一个称号的「首位」文案：`阿米娅 ×2` / `玫兰莎 12 只` / `今日无人获得`。"""
    if board is None or not board.holders:
        return "今日无人获得"
    top = board.holders[0]
    if board.kind == "leaks" or top.leaks > 0:
        return f"{top.name} {top.leaks} 只"
    text = f"{top.name} ×{top.count}"
    if top.best_rounds:
        text += f"（最远 {top.best_rounds} 回合）"
    return text


@dataclass(frozen=True)
class LiveItem:
    """实时状态条里的一格：``在线 12 人``（``cls`` 决定数字配色：num / gold / plain）。"""

    label: str
    value: str
    cls: str = "num"


@dataclass(frozen=True)
class LiveSnapshot:
    """一屏能显示完的服务器实时状态。"""

    online: int = 0
    in_match: int = 0
    waiting: int = 0
    in_hall: int = 0
    spectators: int = 0
    rooms: int = 0
    matches: int = 0

    has_daily: bool = False
    date: str = ""
    today_matches: int = 0
    today_victories: int = 0
    today_win_rate: float = 0.0
    today_gold: int = 0
    today_leaks: int = 0
    today_players: int = 0
    #: 称号 id → 首位文案（只放服务端给了的榜）
    titles: Dict[str, str] = field(default_factory=dict)

    @property
    def online_line(self) -> str:
        parts = [f"在线 {self.online} 人", f"对局中 {self.in_match}"]
        if self.waiting:
            parts.append(f"待开局 {self.waiting}")
        if self.in_hall:
            parts.append(f"大厅 {self.in_hall}")
        if self.spectators:
            parts.append(f"观战 {self.spectators}")
        return " · ".join(parts)

    @property
    def daily_line(self) -> str:
        if not self.has_daily:
            return ""
        parts = [f"今日 {self.today_matches} 局"]
        if self.today_matches:
            parts.append(f"胜率 {self.today_win_rate * 100:.1f}%")
        parts.append(f"消耗资金 {self.today_gold:,}")
        if self.today_leaks:
            parts.append(f"漏怪 {self.today_leaks}")
        return " · ".join(parts)

    def title_lines(self) -> list:
        """按固定顺序返回 `(中文名, 文案)`，跳过服务端没给的榜。"""
        out = []
        for title_id in TITLE_ORDER:
            if title_id in self.titles:
                out.append((TITLE_NAMES[title_id], self.titles[title_id]))
        return out

    @property
    def title_rows(self) -> list:
        """今日战报的结构化行：``{id, name, text, accent}``（accent 取自游戏调色板）。"""
        rows = []
        for title_id in TITLE_ORDER:
            if title_id not in self.titles:
                continue
            rows.append({
                "id": title_id,
                "name": TITLE_NAMES[title_id],
                "text": self.titles[title_id],
                "accent": TITLE_ACCENTS.get(title_id, "mint"),
            })
        return rows

    @property
    def online_items(self) -> list:
        """在线部分的结构化格子（给模板画 HUD 用）。"""
        items = [LiveItem("在线", f"{self.online} 人"), LiveItem("对局中", str(self.in_match))]
        if self.waiting:
            items.append(LiveItem("待开局", str(self.waiting)))
        if self.in_hall:
            items.append(LiveItem("大厅", str(self.in_hall)))
        if self.spectators:
            items.append(LiveItem("观战", str(self.spectators)))
        return items

    @property
    def daily_items(self) -> list:
        """今日战报的结构化格子（消耗资金用金色，与游戏里的价格同色）。"""
        if not self.has_daily:
            return []
        items = [LiveItem("今日", f"{self.today_matches} 局")]
        if self.today_matches:
            items.append(LiveItem("胜率", f"{self.today_win_rate * 100:.1f}%"))
        items.append(LiveItem("消耗资金", f"{self.today_gold:,}", "gold"))
        if self.today_leaks:
            items.append(LiveItem("漏怪", str(self.today_leaks)))
        return items


class LiveData:
    """带缓存与降级的实时数据源（一个进程一个就够）。"""

    def __init__(
        self,
        service: StrongholdService,
        *,
        ttl: float = DEFAULT_TTL,
        failure_ttl: float = DEFAULT_FAILURE_TTL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.service = service
        self.ttl = max(0.0, ttl)
        self.failure_ttl = max(0.0, failure_ttl)
        self.timeout = max(0.1, timeout)
        self._value: Optional[LiveSnapshot] = None
        self._expires = 0.0
        self._inflight: Optional[asyncio.Future] = None

    # ---- 对外 ------------------------------------------------------------------------------------

    async def snapshot(self) -> Optional[LiveSnapshot]:
        """取一份实时快照；不可用时返回 ``None``（调用方显示「实时数据不可用」）。"""
        if time.monotonic() < self._expires:
            return self._value

        inflight = self._inflight
        if inflight is not None:
            # 并发去重：同一时间只打一次服务端
            return await asyncio.shield(inflight)

        task = asyncio.ensure_future(self._load())
        self._inflight = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight is task:
                self._inflight = None

    def invalidate(self) -> None:
        self._expires = 0.0

    # ---- 内部 ------------------------------------------------------------------------------------

    async def _load(self) -> Optional[LiveSnapshot]:
        try:
            status = await asyncio.wait_for(self.service.status(), self.timeout)
        except Exception:  # noqa: BLE001 - 超时/连不上/未实现：菜单只是少显示一块
            self._cache(None, self.failure_ttl)
            return None

        snapshot = self._from_status(status)
        try:
            report = await asyncio.wait_for(self.service.daily(), self.timeout)
        except Exception:  # noqa: BLE001 - 日报拿不到就只显示在线部分
            report = None
        if report is not None:
            snapshot = self._with_daily(snapshot, report)

        self._cache(snapshot, self.ttl)
        return snapshot

    def _cache(self, value: Optional[LiveSnapshot], ttl: float) -> None:
        self._value = value
        self._expires = time.monotonic() + ttl

    @staticmethod
    def _from_status(status: ServerStatus) -> LiveSnapshot:
        counts = status.counts
        return LiveSnapshot(
            online=counts.online,
            in_match=counts.in_match,
            waiting=counts.waiting,
            in_hall=counts.in_hall,
            spectators=counts.spectators,
            rooms=counts.rooms,
            matches=counts.matches,
        )

    @staticmethod
    def _with_daily(snapshot: LiveSnapshot, report: DailyReport) -> LiveSnapshot:
        summary = report.summary
        matches = summary.matches or summary.finished
        rate = summary.win_rate
        if matches and rate <= 0:
            rate = summary.victories / matches
        titles: Dict[str, str] = {}
        for title_id in TITLE_ORDER:
            board = report.board(title_id)
            if board is not None:
                titles[title_id] = _first_holder_text(title_id, board)
        return replace(
            snapshot,
            has_daily=True,
            date=report.date,
            today_matches=matches,
            today_victories=summary.victories,
            today_win_rate=rate,
            today_gold=summary.gold_spent,
            today_leaks=summary.leaks,
            today_players=summary.players,
            titles=titles,
        )


_live: Optional[LiveData] = None


def get_live() -> LiveData:
    """进程内单例（用当前插件的服务对象与配置）。"""
    global _live
    if _live is None:
        from ..runtime import get_service

        service = get_service()
        config = service.config
        _live = LiveData(
            service,
            ttl=float(config.stronghold_menu_live_ttl),
            timeout=float(config.stronghold_menu_timeout),
        )
    return _live


def reset_live() -> None:
    """测试用：丢弃单例。"""
    global _live
    _live = None
