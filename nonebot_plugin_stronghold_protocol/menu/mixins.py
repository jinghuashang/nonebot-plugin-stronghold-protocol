"""把服务器实时状态注入到「卫戍协议」这一项的**详情页**（对任何模板都生效，包括内置 default）。

用**全局** `plugin_detail_mixins` 而不是 `self_detail_mixins`：
`self_*` 靠「当前正在加载的插件」定位自己（`get_matcher_source` 读 NoneBot 的 `_current_plugin`），
而我们的注册发生在 `on_startup`（避免重复加载 picmenu-next），那时上下文不在插件加载里，
会被登记到 picmenu-next 名下而永远不生效。全局收集器没有这个问题，代价是每个插件的详情页都会调用
我们一次 —— 函数开头就按 `plugin_id` 早退，不碰网络。

只改我们自己那一项，且必须 `model_copy()`：picmenu-next 传进来的是全局菜单数据的对象引用，
原地改会污染之后每一次渲染（上游文档的 CAUTION）。
"""

from __future__ import annotations

from nonebot import logger
from nonebot_plugin_picmenu_next.data_source.mixin import plugin_detail_mixins
from nonebot_plugin_picmenu_next.data_source.models import PMNPluginInfo

from ..runtime import get_service
from . import TEMPLATE_NAME
from .live import LiveSnapshot, get_live

#: 我们在 NoneBot 里的插件模块名（picmenu-next 的 plugin_id 就是它）。
PLUGIN_ID = "nonebot_plugin_stronghold_protocol"


def _is_ours(info: PMNPluginInfo) -> bool:
    return info.plugin_id == PLUGIN_ID


def render_live_markdown(live: LiveSnapshot) -> str:
    """实时状态 / 今日战报的 Markdown 片段（不用表格：避免依赖 markdown 的表格扩展）。"""
    lines = ["### 🛡 服务器实时状态", f"- {live.online_line}"]
    daily = live.daily_line
    if daily:
        lines.append(f"- {daily}")
    if live.has_daily:
        lines.append("")
        lines.append(f"**今日战报（{live.date}）**")
        for name, text in live.title_lines():
            lines.append(f"- {name}：{text}")
    return "\n".join(lines)


@plugin_detail_mixins()
async def _inject_live(next_mixin, info: PMNPluginInfo):
    if not _is_ours(info):
        return await next_mixin(info)
    if info.pmn.template == TEMPLATE_NAME:
        # 我们的详情页自己画结构化的 HUD（见 menu/res/detail.html.jinja），不必再塞一份 Markdown
        return await next_mixin(info)
    config = get_service().config
    if not config.stronghold_menu_live:
        return await next_mixin(info)

    try:
        live = await get_live().snapshot()
    except Exception as e:  # noqa: BLE001 - 菜单渲染不能因为取数失败而炸
        logger.warning(f"[stronghold] 菜单实时数据获取失败：{type(e).__name__}: {e}")
        live = None

    if live is None:
        return await next_mixin(info)

    info = info.model_copy()
    info.description = f"{info.description or ''}\n\n{render_live_markdown(live)}".strip()
    return await next_mixin(info)
