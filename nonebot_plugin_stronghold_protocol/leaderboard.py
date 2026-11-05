"""从游戏服 WebSocket 读排行榜（``board.get`` → ``board.state``）。

服务器自己的排行榜一直有「兜底之王」（``unite``，按每局结算里的 ``stats.uniteKills`` 累计），
而桥接的 ``/daily`` 只有在服务端进程加载了新代码之后才带这个榜 —— 日报要用它时直接读服务器更省事，
**不用重启服务端**。读到的是累计口径，卡片上会标注「累计」。

一次读榜 = 连上 → ``hello`` → ``board.get`` → 拿到 ``board.state`` → 断开；结果缓存几分钟，
避免每次命令都占一个游戏会话。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from nonebot import logger

from .models import TitleBoard, TitleHolder

#: 合并哪些分场：单人 + 多人；娱乐房（fun）里常有 AI 上榜，不混进真人榜。
UNITE_SCOPES = ("solo", "multi")

#: `hello.name`：服务器会把它当昵称回显，起个一眼看得出是统计用的名字。
DEFAULT_NAME = "卫戍统计"


def ws_url_from(base_url: str) -> str:
    """``https://sp.example.com`` → ``wss://sp.example.com/ws``（已带 /ws 就不重复加）。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    if not url.endswith("/ws"):
        url += "/ws"
    return url


def merge_unite(boards: List[Dict[str, Any]]) -> Optional[TitleBoard]:
    """把 ``solo`` / ``multi`` 两个分场的 ``unite`` 榜合成一张（按累计击退降序）。

    纯函数，方便单测：输入是 ``board.state.boards`` 的原始数组。
    """
    rows: Dict[str, TitleHolder] = {}
    for board in boards or []:
        if not isinstance(board, dict) or board.get("id") != "unite":
            continue
        if board.get("scope") not in UNITE_SCOPES:
            continue
        for raw in board.get("rows") or []:
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get("playerId") or raw.get("name") or "").strip()
            if not ident:
                continue
            row = rows.get(ident)
            if row is None:
                row = TitleHolder(player_id=str(raw.get("playerId") or ""), name=str(raw.get("name") or ""))
                rows[ident] = row
            row.kills += int(raw.get("kills") or 0)
            row.best = max(row.best, int(raw.get("best") or 0))
            row.matches += int(raw.get("matches") or 0)
            row.last_at = max(row.last_at, int(raw.get("lastAt") or 0))
            if raw.get("name"):
                row.name = str(raw["name"])

    holders = [h for h in rows.values() if h.kills > 0]
    if not holders:
        return None
    holders.sort(key=lambda h: (h.kills, h.best, h.name), reverse=True)
    return TitleBoard(
        id="unite",
        name="兜底之王",
        note="联合防守中击退敌人最多（累计）",
        kind="unite",
        total=len(holders),
        holders=holders,
    )


class LeaderboardClient:
    """一次性的 WS 读榜客户端（带缓存）。读失败只告警，绝不影响日报本身。"""

    def __init__(
        self,
        url: str,
        *,
        name: str = DEFAULT_NAME,
        timeout: float = 6.0,
        cache_seconds: float = 300.0,
        verify_tls: bool = True,
    ):
        self.url = url
        self.name = name
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.verify_tls = verify_tls
        self._cached: Optional[TitleBoard] = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def unite(self) -> Optional[TitleBoard]:
        if not self.url:
            return None
        if self._cached is not None and time.monotonic() - self._cached_at < self.cache_seconds:
            return self._cached
        async with self._lock:
            if self._cached is not None and time.monotonic() - self._cached_at < self.cache_seconds:
                return self._cached
            try:
                boards = await self._fetch()
            except Exception as e:  # noqa: BLE001 - 排行榜是锦上添花，失败不能拖垮日报
                logger.warning(f"[stronghold] 读取排行榜失败，跳过兜底之王：{type(e).__name__}: {e}")
                return self._cached
            self._cached = merge_unite(boards)
            self._cached_at = time.monotonic()
            return self._cached

    async def _fetch(self) -> List[Dict[str, Any]]:
        import aiohttp  # 延迟导入：没装也不影响其它命令

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        ssl_arg: Any = None if self.verify_tls else False
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(self.url, ssl=ssl_arg, heartbeat=None) as ws:
                await ws.send_json({"t": "hello", "rid": 1, "name": self.name, "version": 1, "delta": 1})
                asked = False
                while True:
                    msg = await asyncio.wait_for(ws.receive(), self.timeout)
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        raise RuntimeError("websocket closed before board.state")
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("t") == "board.state":
                        return list(data.get("boards") or [])
                    if data.get("t") == "helloError":
                        raise RuntimeError(str(data.get("reason") or "hello rejected"))
                    # 服务器不会主动推榜：welcome 之后要自己问一次 board.get
                    if data.get("t") == "welcome" and not asked:
                        asked = True
                        await ws.send_json({"t": "board.get", "rid": 2})
