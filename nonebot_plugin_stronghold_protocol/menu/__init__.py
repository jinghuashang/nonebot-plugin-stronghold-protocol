"""可选子包：`nonebot-plugin-picmenu-next` 图片菜单（模板名 ``stronghold``）。

没装 picmenu-next（或 Python < 3.10、或它加载失败）时整包静默跳过 —— 插件本体不依赖它。

⚠️ 两条硬性约束（都是实测踩出来的）：

1. **注册必须延迟到 ``on_startup``**。在插件导入期 ``require("nonebot_plugin_picmenu_next")`` 没问题，
   但 bot 随后还会从 ``pyproject.toml`` 再加载一次它，NoneBot 会抛
   ``RuntimeError: Plugin already exists: nonebot_plugin_localstore`` 并**重启循环**。
   （线上部署时就是这么崩的；``pmn_archive_theme`` 也为此把注册放到启动钩子里。）
2. **必须走 ``nonebot.require`` 而不是 ``import``**：直接 import 会把 NoneBot 插件塞进 ``sys.modules``，
   之后任何人再 ``require`` 它都会报「不是以插件方式加载」。
"""

from __future__ import annotations

import importlib.util
import sys

from nonebot import get_driver, logger

#: picmenu-next 要求 Python >= 3.10（本插件本体支持 3.9）。
PMN_MIN_PYTHON = (3, 10)
PMN_MODULE = "nonebot_plugin_picmenu_next"
#: 我们注册的模板名（`extra["pmn"]["template"]` 与 `PMN_*_TEMPLATE` 都用它）。
TEMPLATE_NAME = "stronghold"

driver = get_driver()

_registered = False


def available() -> bool:
    """picmenu-next 是否装了（只探测 ``find_spec``，不 import、不 require）。"""
    if sys.version_info < PMN_MIN_PYTHON:
        return False
    try:
        return importlib.util.find_spec(PMN_MODULE) is not None
    except (ImportError, ValueError):
        return False


def register_menu_templates() -> bool:
    """把 ``stronghold`` 模板与实时数据 mixin 注册进 picmenu-next（幂等）。

    :returns: 是否已经就绪（False = 没装 picmenu-next，或注册失败，菜单会回退到别的模板）
    """
    global _registered
    if _registered:
        return True
    if not available():
        if sys.version_info < PMN_MIN_PYTHON:
            logger.info("[stronghold] Python < 3.10，跳过往图片菜单（picmenu-next 需要 3.10+）")
        else:
            logger.debug("[stronghold] 未安装 nonebot-plugin-picmenu-next，跳过图片菜单")
        return False

    from nonebot import require

    try:
        require(PMN_MODULE)
    except Exception as e:  # noqa: BLE001 - 缺依赖、cookit 的 localstore 策略、版本不符……
        logger.warning(
            f"[stronghold] 图片菜单不可用（{type(e).__name__}: {e}）；"
            "插件本体不受影响，详见 docs/MENU.md"
        )
        return False

    from . import template  # noqa: F401  —— 导入即注册 stronghold 模板

    try:
        # mixin 靠「本插件是否已注册」定位自己；失败只是少了实时状态注入
        from . import mixins  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[stronghold] 菜单实时数据未注入（{type(e).__name__}: {e}）")

    _registered = True
    return True


@driver.on_startup
async def _register_menu_on_startup() -> None:
    """启动时注册（此时所有插件都已加载完，不会再撞上重复加载）。"""
    register_menu_templates()


def reset_registration() -> None:
    """测试用：忘掉「已注册」状态。"""
    global _registered
    _registered = False
