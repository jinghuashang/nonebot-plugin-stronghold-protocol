"""每日定时推送：按配置时区的 HH:MM 取一次日报，发到配置的群。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from nonebot import get_bots, logger

from .bridge import BridgeError
from .cards import build_image
from .cards.render import image_message
from .cards.views import daily_view
from .config import Config
from .render import describe_error, render_daily
from .service import StrongholdService

DEFAULT_REPORT_TIME = (11, 0)


def parse_hhmm(text: str) -> Optional[tuple]:
    """``"11:00"`` → ``(11, 0)``；非法返回 None。"""
    raw = (text or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


class DailyReportScheduler:
    """进程内的小调度器：睡到下一个 HH:MM → 推一次 → 再睡。

    没有引入 apscheduler：一天只触发一次，一个 ``asyncio`` 任务足够了，也少一个依赖。
    """

    def __init__(self, service: StrongholdService, config: Config):
        self.service = service
        self.config = config
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._warned = False

    # ---- 时间 ------------------------------------------------------------------------------------

    @property
    def time_of_day(self) -> tuple:
        parsed = parse_hhmm(self.config.stronghold_report_time)
        if parsed is None:
            if not self._warned:
                self._warned = True
                fallback = f"{DEFAULT_REPORT_TIME[0]:02d}:{DEFAULT_REPORT_TIME[1]:02d}"
                logger.warning(
                    "[stronghold] stronghold_report_time 配置无效 "
                    f"({self.config.stronghold_report_time!r})，按 {fallback} 推送"
                )
            return DEFAULT_REPORT_TIME
        return parsed

    def next_fire_at(self, now: Optional[datetime] = None) -> datetime:
        """严格晚于 now 的下一个推送时刻（本地化到配置时区）。"""
        current = now or datetime.now(self.service.tz)
        hour, minute = self.time_of_day
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target

    # ---- 生命周期 --------------------------------------------------------------------------------

    def start(self) -> None:
        if not self.config.stronghold_report_enabled:
            logger.info("[stronghold] 日报定时推送已关闭（stronghold_report_enabled=false）")
            return
        if self._task and not self._task.done():
            return
        from .runtime import command_prefix

        groups = self.service.report_groups()
        if groups:
            logger.info(f"[stronghold] 日报推送目的地：{groups}")
        else:
            logger.info(
                f"[stronghold] 还没有订阅日报的群：管理员在群里发 {command_prefix()}日报 开启 即可"
            )
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="stronghold-daily-report")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停机时不关心取消原因
                pass

    async def _loop(self) -> None:
        while not self._stopped:
            target = self.next_fire_at()
            delay = max(1.0, (target - datetime.now(self.service.tz)).total_seconds())
            logger.info(
                f"[stronghold] 下一份日报：{target.strftime('%Y-%m-%d %H:%M')}"
                f"（{self.config.stronghold_report_tz}）"
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if self._stopped:
                return
            await self.fire()

    # ---- 推送 ------------------------------------------------------------------------------------

    async def fire(self) -> List[str]:
        """取一次日报并推送；返回每个目标的结果描述（日志/测试用）。"""
        results: List[str] = []
        # 群列表现算：管理员随时可以在群里开/关，不用重启。
        groups = self.service.report_groups()
        if not groups:
            logger.info("[stronghold] 没有订阅日报的群，跳过本次推送")
            return results
        try:
            report = await self.service.daily()
        except BridgeError as e:
            logger.error(f"[stronghold] 日报取数失败：{describe_error(e)}")
            return ["取数失败"]

        text = render_daily(report, max_titles=self.config.stronghold_report_max_titles, tz=self.service.tz)
        image = await build_image(
            "daily",
            daily_view(report, max_titles=self.config.stronghold_report_max_titles, tz=self.service.tz),
        )
        for line in await broadcast(text, groups, image=image):
            results.append(line)
            logger.info(f"[stronghold] 日报推送：{line}")
        return results


async def broadcast(text: str, groups: Sequence[int], image: Optional[bytes] = None) -> List[str]:
    """把日报发到每个群（每个已连接机器人都试一遍），返回 ``成功/失败`` 描述。

    有图片就发图片，某个群发图失败时**该群**单独回退文本；图片为空则直接发文本。
    """
    results: List[str] = []
    bots = list(get_bots().values())
    if not bots:
        return ["没有已连接的机器人，跳过推送"]
    for bot in bots:
        for group in groups:
            sent = False
            if image:
                message = image_message(bot, image)
                if message is not None:
                    try:
                        await bot.send_group_msg(group_id=int(group), message=message)  # type: ignore[attr-defined]
                        results.append(f"{group} 已发送（图片）")
                        sent = True
                    except Exception as e:  # noqa: BLE001 - 回退文本
                        logger.warning(f"[stronghold] {group} 发图失败，改用文本：{type(e).__name__}: {e}")
            if sent:
                continue
            try:
                await bot.send_group_msg(group_id=int(group), message=text)  # type: ignore[attr-defined]
                results.append(f"{group} 已发送")
            except Exception as e:  # noqa: BLE001 - 一个群发不出去不该影响其他群
                results.append(f"{group} 发送失败（{type(e).__name__}）")
    return results
