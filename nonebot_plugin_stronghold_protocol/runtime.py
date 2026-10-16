"""运行时单例：插件启动时把 ``StrongholdService`` 放进来，命令模块通过它取。"""

from __future__ import annotations

from typing import Optional

from .service import StrongholdService

_service: Optional[StrongholdService] = None


def command_prefix() -> str:
    """指令前缀：优先空串（裸词可用），否则第一个非空前缀。

    很多部署的 ``COMMAND_START`` 含 ``""``（用户直接敲 ``在线``），菜单与命令提示都必须跟着变，
    否则机器人会教用户敲一条敲不出来的命令。
    """
    try:
        from nonebot import get_driver

        starts = get_driver().config.command_start
    except Exception:  # noqa: BLE001 - 没初始化/配置缺失都退回 '/'
        return "/"
    if isinstance(starts, (set, list, tuple)):
        if "" in starts:
            return ""
        for item in starts:
            if isinstance(item, str) and item:
                return item
    return "/"


def set_service(service: StrongholdService) -> None:
    global _service
    _service = service


def get_service() -> StrongholdService:
    if _service is None:  # 插件导入顺序错了才会走到；正常由 __init__ 先设置
        raise RuntimeError("StrongholdService 尚未初始化")
    return _service
