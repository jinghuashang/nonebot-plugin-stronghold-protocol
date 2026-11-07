"""服务器公告同步：轮询 ``/api/announcements``，把新公告渲染成卡片发到白名单群。

服务器端（游戏服）发布公告后，客户端下次刷新就会看到；QQ 群这边由这里轮询补上：

- 只发**新出现**的公告（按 id 记在 ``<data_dir>/announcements-seen.json``，重启不重复）；
- 首次启动默认**不补发**旧公告（``stronghold_announce_backfill`` 可打开）；
- 过期的（``until`` 已过）直接跳过；
- 发到 ``stronghold_manage_groups`` 白名单群，出图失败自动回退文本。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

from nonebot import get_bots, logger

from .bridge import BridgeError
from .cards import build_image, views
from .scheduler import broadcast
from .service import StrongholdService
from .storage import default_data_dir

STATE_VERSION = 1

#: 公告等级 → 卡片语气。
TONE_BY_LEVEL = {"critical": "error", "warn": "warn", "info": "info"}


def _title(item: Dict[str, Any]) -> str:
    return str(item.get("title") or "").strip() or "服务器公告"


def _body(item: Dict[str, Any]) -> str:
    return str(item.get("body") or "").strip()


def _expired(item: Dict[str, Any], now_ms: float) -> bool:
    until = item.get("until")
    return isinstance(until, (int, float)) and until > 0 and until <= now_ms


class AnnouncementPoller:
    """每 ``stronghold_announce_poll`` 秒看一次服务器公告。"""

    def __init__(self, service: StrongholdService):
        self.service = service
        self.config = service.config
        self.path = default_data_dir(service.config.stronghold_data_dir) / "announcements-seen.json"
        self._seen: Dict[str, float] = {}
        self._loaded = False
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    # ---- 状态文件 --------------------------------------------------------------------------------

    def _load(self) -> None:
        self._loaded = True
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        seen = doc.get("seen") if isinstance(doc, dict) else None
        if isinstance(seen, dict):
            self._seen = {str(k): float(v) for k, v in seen.items() if isinstance(v, (int, float))}

    def _save(self) -> None:
        doc = {"version": STATE_VERSION, "savedAt": int(time.time() * 1000), "seen": self._seen}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- 生命周期 --------------------------------------------------------------------------------

    def start(self) -> None:
        if not self.config.stronghold_announce_push:
            logger.info("[stronghold] 公告同步已关闭（stronghold_announce_push=false）")
            return
        if self._task and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="stronghold-announce-poll")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停机不关心取消原因
                pass

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self.poll()
            except Exception as e:  # noqa: BLE001 - 一轮失败不能拖垮轮询
                logger.warning(f"[stronghold] 公告同步失败：{type(e).__name__}: {e}")
            try:
                await asyncio.sleep(max(15.0, float(self.config.stronghold_announce_poll)))
            except asyncio.CancelledError:
                return

    # ---- 同步一次 --------------------------------------------------------------------------------

    async def poll(self, *, allow_backfill: Optional[bool] = None) -> List[str]:
        """拉一次公告：新的发到白名单群，返回每个目标的发送结果（日志/测试用）。"""
        groups = [int(g) for g in self.config.stronghold_manage_groups]
        if not groups:
            return []
        if not get_bots():
            # 机器人还没连上：这一轮什么都别做，更不能把公告记成「已发送」
            return []
        if not self._loaded:
            self._load()

        first_run = not self.path.exists()
        if allow_backfill is None:
            allow_backfill = bool(self.config.stronghold_announce_backfill)

        try:
            items = await self.service.client.announcements()
        except BridgeError as e:
            logger.warning(f"[stronghold] 读取服务器公告失败：{e}")
            return []

        now_ms = time.time() * 1000
        active = [a for a in items if not _expired(a, now_ms)]
        unseen = [a for a in active if str(a.get("id") or "") and str(a.get("id")) not in self._seen]
        if first_run:
            # 首次启动：默认只把现有公告记为已读；开了补发也只补**最新的一条**，避免一次刷十几张卡
            unseen = [max(active, key=lambda a: a.get("at") or 0)] if (allow_backfill and active) else []

        results: List[str] = []
        for item in unseen:
            announce_id = str(item.get("id"))
            title, body = _title(item), _body(item)
            tone = TONE_BY_LEVEL.get(str(item.get("level") or ""), "info")
            view = views.note_view(title, body or "（公告没有正文）", tone, crumb="卫戍协议 / 公告")
            image = await build_image("note", view)
            text = f"📢 服务器公告 · {title}\n{body}"
            for line in await broadcast(text, groups, image=image):
                results.append(line)
                logger.info(f"[stronghold] 公告推送 {announce_id}：{line}")
            self._seen[announce_id] = now_ms

        if first_run or unseen:
            for item in active:
                announce_id = str(item.get("id") or "")
                if announce_id:
                    self._seen.setdefault(announce_id, now_ms)
            self._save()
        return results
