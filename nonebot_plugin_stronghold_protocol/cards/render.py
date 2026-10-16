"""卡片渲染：Jinja 模板 → HTML → 截图 → 图片字节。

- 只依赖可选的 `nonebot-plugin-htmlrender`（没装、浏览器起不来、渲染抛错，一律返回 `None`，
  调用方回退文本 —— 见 `cards.reply`）。
- HTML 自带内联 CSS（不额外起静态服务），所以页面是完全自包含的。
- 必须走 `nonebot.require`（由插件管理器加载），直接 `import nonebot_plugin_htmlrender` 会在
  它的 localstore 调用方检测那里炸掉。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, Optional

import jinja2 as jj
from nonebot import logger

from .views import WIDTH

RES_DIR = Path(__file__).parent / "res"
HR_MODULE = "nonebot_plugin_htmlrender"
#: 内容判空用的最小 HTML（截图高度自适应由 Playwright 的 full_page 处理）。
VIEWPORT = {"width": WIDTH, "height": 40}

jj_env = jj.Environment(loader=jj.FileSystemLoader(RES_DIR), autoescape=True, enable_async=True)

_css: Optional[str] = None
_warned: set[str] = set()


def available() -> bool:
    """htmlrender 装了没（只探测，不 import）。"""
    try:
        return importlib.util.find_spec(HR_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _load_css() -> str:
    global _css
    if _css is None:
        _css = (RES_DIR / "card.css").read_text(encoding="utf-8")
    return _css


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


def _brand() -> str:
    """页脚左侧：服务器名 + 插件名与版本（配置读不到就只写插件名）。"""
    from .. import __version__

    name = ""
    try:
        from ..runtime import get_service

        name = get_service().config.stronghold_menu_server_name or ""
    except Exception:  # noqa: BLE001
        name = ""
    plugin = f"nonebot-plugin-stronghold-protocol v{__version__}"
    return f"{name} · {plugin}" if name else plugin


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M")


async def render_html(kind: str, context: Dict[str, Any]) -> Optional[str]:
    """渲染出 HTML（不截图），失败返回 None。测试与排查都用得上。"""
    try:
        template = jj_env.get_template(f"{kind}.html.jinja")
        payload: Dict[str, Any] = {"brand": _brand(), "generated_at": _now_text(), **context}
        return await template.render_async(css=_load_css(), **payload)
    except Exception as e:  # noqa: BLE001 - 模板问题不能让回复消失
        _warn_once(f"html:{kind}", f"[stronghold] 卡片模板 {kind} 渲染失败，改用文本：{type(e).__name__}: {e}")
        return None


async def render(kind: str, context: Dict[str, Any]) -> Optional[bytes]:
    """把一张卡片渲染成 JPEG 字节；任何一环失败都返回 ``None``。"""
    if not available():
        _warn_once("missing", "[stronghold] 未安装 nonebot-plugin-htmlrender，命令回复将使用文本")
        return None

    html = await render_html(kind, context)
    if html is None:
        return None

    try:
        from nonebot import require

        require(HR_MODULE)
        import nonebot_plugin_htmlrender as htmlrender  # noqa: PLC0415

        # 各版本的"一站式"接口名字变过：0.7.x 的 render_html → 0.6/早期 0.7 的 html_to_pic。
        for name in ("render_html", "html_to_pic"):
            shot = getattr(htmlrender, name, None)
            if not callable(shot):
                continue
            try:
                return await shot(html, type="jpeg", quality=92, full_page=True, viewport=VIEWPORT)
            except TypeError:  # 该版本不吃这些参数 → 换下一页那条路
                break

        # 兜底：自己起页面截图（0.6 ~ 0.8 都有这套页面 API）
        context = getattr(htmlrender, "get_render_context", None) or htmlrender.get_new_page
        async with context(viewport=VIEWPORT) as page:
            await page.set_content(html)
            return await page.screenshot(full_page=True, type="jpeg", quality=92)
    except Exception as e:  # noqa: BLE001 - 浏览器/渲染后端问题都退化到文本
        _warn_once(f"render:{kind}", f"[stronghold] 卡片 {kind} 截图失败，改用文本：{type(e).__name__}: {e}")
        return None


def image_message(target: Any, data: bytes) -> Optional[Any]:
    """按目标所属适配器拼一条图片消息。

    ``target`` 可以是事件（用它自己的消息类）或机器人（用所属适配器包的 ``MessageSegment``）；
    拿不到就返回 ``None``（调用方回退文本）。

    注意不能靠 ``hasattr(target, "get_message")`` 判断：``Bot`` 也有一个（异步的）``get_message``，
    会把机器人当成事件、拿到一个协程。
    """
    from nonebot.adapters import Bot, Event

    try:
        message_cls = None
        if isinstance(target, Event):
            message_cls = type(target.get_message())
        elif isinstance(target, Bot):
            package = type(target.adapter).__module__.rsplit(".", 1)[0]
            message_cls = getattr(importlib.import_module(package), "Message", None)
        if message_cls is None:
            return None
        segment_cls = message_cls.get_segment_class()
        image = getattr(segment_cls, "image", None)
        if image is None:
            return None
        return message_cls(image(data))
    except Exception:  # noqa: BLE001 - 不认识的适配器走文本
        return None
