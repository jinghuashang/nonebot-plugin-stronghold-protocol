"""图片菜单模板 ``stronghold``：比 picmenu-next 内置的 ``default`` 多出统计条、实时状态条、
分组卡片、功能表与页脚生成时间。

渲染管线照抄上游 default 的做法（MIT，来源：nonebot-plugin-picmenu-next
`nonebot_plugin_picmenu_next/templates/default/__init__.py`），只把 KaTeX 的路由修正为正确映射、
并把视图数据整理成模板好用的形状。
"""

from __future__ import annotations

from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import jinja2 as jj
from cookit import DebugFileWriter
from cookit.pw import RouterGroup, make_real_path_router, screenshot_html
from cookit.pw.loguru import log_router_err
from cookit.pyd.compat import get_model_with_config
from nonebot import get_plugin_config, logger, require
from nonebot_plugin_alconna.uniseg import UniMessage
from nonebot_plugin_picmenu_next.data_source.models import PMDataItem, PMNPluginInfo, compat_model_config
from nonebot_plugin_picmenu_next.markdown import build_default_prp_processor
from nonebot_plugin_picmenu_next.templates import detail_templates, func_detail_templates, index_templates
from nonebot_plugin_picmenu_next.templates.jj_utils import build_base_render_kwargs, filters
from nonebot_plugin_picmenu_next.templates.pw_utils import (
    ROUTE_BASE_URL,
    base_routers,
    local_file_route_prp_transformer,
)
from pydantic import Field

if TYPE_CHECKING:
    from playwright.async_api import Page
    from yarl import URL

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import get_new_page  # noqa: E402
from nonebot_plugin_picmenu_next.templates.hr_utils import HTMLRENDER_KATEX_DIR  # noqa: E402

from ..runtime import get_service  # noqa: E402
from . import TEMPLATE_NAME  # noqa: E402
from .live import LiveSnapshot, get_live  # noqa: E402

#: 本插件的 NoneBot 模块名（用来判断菜单项是不是「我们自己」）。
PLUGIN_ID = "nonebot_plugin_stronghold_protocol"
#: 页脚显示的分发名与版本（模板会渲染成 `<名字> v<版本>`）。
PLUGIN_DIST = "nonebot-plugin-stronghold-protocol"
#: 画布尺寸（与 pmn 内置 default 一致，便于对照）。
VIEWPORT = {"width": 810, "height": 2430}


def _footer_context() -> Dict[str, str]:
    """页脚用的插件名与版本。"""
    from .. import __version__

    return {"plugin_name": PLUGIN_DIST, "plugin_version": __version__}


# ---- 配置 ---------------------------------------------------------------------------------------

_TemplateBase = get_model_with_config(
    {
        "alias_generator": lambda x: f"pmn_{TEMPLATE_NAME}_{x}",
        **compat_model_config,
    },
)


class TemplateConfigModel(_TemplateBase):
    """模板自己的配置：``pmn_stronghold_*``（``command_start`` 直接沿用 NoneBot 的）。

    默认就是设计语言本身的**近黑墨绿**（Stronghold-Protocol 是 dark-first）；
    ``light`` 只是给亮色聊天窗口留的后路。
    """

    command_start: set = Field(default_factory=lambda: {"/"}, alias="command_start")
    light: bool = False
    additional_css: List[str] = Field(default_factory=list)
    additional_js: List[str] = Field(default_factory=list)

    @cached_property
    def pfx(self) -> str:
        return next(iter(self.command_start), "")


template_config = get_plugin_config(TemplateConfigModel)


# ---- 常量 ---------------------------------------------------------------------------------------

RES_DIR = Path(__file__).parent / "res"
debug = DebugFileWriter(Path.cwd() / "debug", "picmenu-next", TEMPLATE_NAME)


# ---- Jinja --------------------------------------------------------------------------------------

jj_env = jj.Environment(loader=jj.FileSystemLoader(RES_DIR), autoescape=True, enable_async=True)
jj_env.filters.update(filters.data)

prp_processor = build_default_prp_processor(local_file_route_prp_transformer)


# ---- 资源路由 -----------------------------------------------------------------------------------

static_routers = base_routers.copy()


@static_routers.router(f"{ROUTE_BASE_URL}/katex/**/*")
@make_real_path_router
@log_router_err()
async def _(url: "URL", **_):
    # `/katex/<file>` → htmlrender 的 katex 目录（上游 default 用 `[2:]` + `/markdown/katex/...`，会多算一层）
    return HTMLRENDER_KATEX_DIR.joinpath(*url.parts[2:])


@static_routers.router(f"{ROUTE_BASE_URL}/**/*", 99)
@make_real_path_router
@log_router_err()
async def _(url: "URL", **_):
    return RES_DIR.joinpath(*url.parts[1:])


# ---- 视图数据 -----------------------------------------------------------------------------------


def _plugin_type(info: PMNPluginInfo) -> str:
    plugin = info.plugin
    metadata = getattr(plugin, "metadata", None)
    kind = getattr(metadata, "type", None)
    return kind if isinstance(kind, str) and kind else "application"


def _card(no: int, info: PMNPluginInfo) -> Dict[str, Any]:
    funcs = info.pm_data or []
    return {
        "no": no,
        "info": info,
        "func_count": len(funcs),
        "visible_func_count": len([x for x in funcs if not x.hidden]),
        "is_ours": info.plugin_id == PLUGIN_ID,
    }


def build_index_view(infos: List[PMNPluginInfo], showing_hidden: bool) -> Dict[str, Any]:
    """首页模板要用的数据（纯函数，方便单测）。"""
    cards = [_card(i + 1, info) for i, info in enumerate(infos)]
    apps = [c for c in cards if _plugin_type(c["info"]) != "library"]
    libs = [c for c in cards if _plugin_type(c["info"]) == "library"]
    groups = []
    if apps:
        groups.append({"title": "应用", "cards": apps})
    if libs:
        groups.append({"title": "类库", "cards": libs})

    func_count = sum(c["func_count"] for c in cards)
    hidden_count = sum(
        1 for c in cards if c["info"].pmn.hidden or c["func_count"] > c["visible_func_count"]
    )
    return {
        "groups": groups,
        "plugin_count": len(cards),
        "func_count": func_count,
        "hidden_count": hidden_count if showing_hidden else 0,
    }


def build_detail_view(info: PMNPluginInfo, func: Optional[PMDataItem] = None) -> Dict[str, Any]:
    """详情页 / 功能详情页模板要用的数据。"""
    funcs = info.pm_data or []
    entries = [{"no": i + 1, "func": x} for i, x in enumerate(funcs)]
    return {
        "func_entries": entries,
        "func_count": len(entries),
        "is_ours": info.plugin_id == PLUGIN_ID,
        "plugin_type": _plugin_type(info),
        "func": func,
        "adapters": sorted(info.supported_adapters) if info.supported_adapters else [],
    }


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


async def _live_snapshot() -> Optional[LiveSnapshot]:
    config = get_service().config
    if not config.stronghold_menu_live:
        return None
    try:
        return await get_live().snapshot()
    except Exception as e:  # noqa: BLE001 - 菜单不能因为取数失败而炸
        logger.warning(f"[stronghold] 菜单实时数据获取失败：{type(e).__name__}: {e}")
        return None


# ---- 渲染 ---------------------------------------------------------------------------------------


async def render_html(template: str, info: Optional[PMNPluginInfo] = None, **kwargs: Any) -> str:
    """把模板渲染成 HTML（不起浏览器，测试里也能用）。"""
    template_obj = jj_env.get_template(template)
    context: Dict[str, Any] = dict(kwargs)
    if info is not None:
        # 模板里要用 `info`（详情页/功能详情页），上游默认模板同样是把它放进上下文
        context["info"] = info
    return await template_obj.render_async(
        cfg=template_config,
        **build_base_render_kwargs(info=info, prp_processor=prp_processor),
        **context,
    )


async def render(template: str, info: Optional[PMNPluginInfo] = None, **kwargs: Any) -> UniMessage:
    html = await render_html(template, info=info, **kwargs)
    if debug.enabled:
        debug.write(html, f"{template.replace('.html.jinja', '')}_{{time}}.html")

    routers: RouterGroup = static_routers.copy()
    async with get_new_page(viewport=VIEWPORT) as page:
        if TYPE_CHECKING:
            assert isinstance(page, Page)
        await routers.apply(page)
        await page.goto(f"{ROUTE_BASE_URL}/")
        pic = await screenshot_html(page, html, selector="main", type="jpeg")
    return UniMessage.image(raw=pic)


async def render_index(
    infos: List[PMNPluginInfo],
    showing_hidden: bool,
    user_can_see_hidden: Optional[bool],
) -> UniMessage:
    view = build_index_view(infos, showing_hidden)
    return await render(
        "index.html.jinja",
        infos=infos,
        showing_hidden=showing_hidden,
        user_can_see_hidden=user_can_see_hidden,
        live=await _live_snapshot(),
        server_name=get_service().config.stronghold_menu_server_name,
        now_text=_now_text(),
        **_footer_context(),
        **view,
    )


async def render_detail(
    info: PMNPluginInfo,
    info_index: int,
    showing_hidden: bool,
    user_can_see_hidden: Optional[bool],
) -> UniMessage:
    return await render(
        "detail.html.jinja",
        info=info,
        info_index=info_index,
        showing_hidden=showing_hidden,
        user_can_see_hidden=user_can_see_hidden,
        live=await _live_snapshot(),
        server_name=get_service().config.stronghold_menu_server_name,
        now_text=_now_text(),
        **_footer_context(),
        **build_detail_view(info),
    )


async def render_func_detail(
    info: PMNPluginInfo,
    info_index: int,
    func: PMDataItem,
    func_index: Optional[int],
    showing_hidden: bool,
    user_can_see_hidden: Optional[bool],
) -> UniMessage:
    return await render(
        "func_detail.html.jinja",
        info=info,
        info_index=info_index,
        func_index=func_index,
        showing_hidden=showing_hidden,
        user_can_see_hidden=user_can_see_hidden,
        live=await _live_snapshot(),
        server_name=get_service().config.stronghold_menu_server_name,
        now_text=_now_text(),
        **_footer_context(),
        # build_detail_view 已经带了 func（模板上下文用），这里不要再传一次
        **build_detail_view(info, func=func),
    )


# ---- 注册 ---------------------------------------------------------------------------------------


def _register(collector, name: str, handler) -> bool:
    """注册一个模板；重名/并发注册都只记 warning（cookit 的收集器重名会直接抛 ValueError）。"""
    if name in collector.data:
        logger.warning(f"[stronghold] 菜单模板 {name} 已被注册，跳过（回退到已注册的那个）")
        return False
    try:
        collector(name)(handler)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[stronghold] 注册菜单模板 {name} 失败：{type(e).__name__}: {e}")
        return False
    return True


_registered = [
    _register(index_templates, TEMPLATE_NAME, render_index),
    _register(detail_templates, TEMPLATE_NAME, render_detail),
    _register(func_detail_templates, TEMPLATE_NAME, render_func_detail),
]

if any(_registered):
    logger.info(
        f"[stronghold] 菜单模板 {TEMPLATE_NAME} 已注册"
        f"（index={_registered[0]} detail={_registered[1]} func_detail={_registered[2]}）"
    )
else:
    logger.warning(f"[stronghold] 菜单模板 {TEMPLATE_NAME} 未注册（重名或异常），详情页会回退到已注册的模板")
