"""命令回复的出图层：把数据渲染成一张卡片图，渲染不了就回退文本。

对外的用法只有两个：

```python
await cards.reply(matcher, event, "daily", cards.views.daily_view(report), fallback_text)
image = await cards.build_image("daily", cards.views.daily_view(report))   # 定时推送用
```
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import render, views
from .render import image_message
from .views import WIDTH

__all__ = ["build_image", "image_message", "render", "reply", "views", "WIDTH"]


def _enabled() -> bool:
    """配置开关：`stronghold_reply_image=false` 时一律回文本（排查时可临时关掉）。"""
    try:
        from ..runtime import get_service

        return bool(get_service().config.stronghold_reply_image)
    except Exception:  # noqa: BLE001 - 配置读不到就当作开启
        return True


async def build_image(kind: str, context: Dict[str, Any]) -> Optional[bytes]:
    """渲染一张卡片；关掉开关 / htmlrender 缺失 / 渲染失败都返回 ``None``。"""
    if not _enabled():
        return None
    return await render.render(kind, context)


async def reply(matcher: Any, event: Any, kind: str, context: Dict[str, Any], text: str) -> None:
    """回复一条消息：优先图片，失败则文本。

    两处都走 ``matcher.finish``（真实运行时它会抛 ``FinishedException`` 结束这次响应；
    显式 ``return`` 是为了让流程在测试里也可读）。
    """
    data = await build_image(kind, context)
    if data:
        message = image_message(event, data)
        if message is not None:
            await matcher.finish(message)
            return
    await matcher.finish(text)
