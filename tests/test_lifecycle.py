"""启动/停机钩子：确认插件在 NoneBot 真正启动时把日报调度器拉起来、停机时收干净。"""

from __future__ import annotations

import nonebot

PLUGIN_NAME = "nonebot_plugin_stronghold_protocol"
GROUP_ID = 123456789


def _plugin():
    plugin = nonebot.get_plugin_by_module_name(PLUGIN_NAME)
    assert plugin is not None, "插件没被加载（conftest 里 load_plugin 失败？）"
    return plugin


def _hooks(kind: str) -> list:
    lifespan = nonebot.get_driver()._lifespan  # noqa: SLF001 - 只能从这里拿到注册的钩子
    return [f for f in getattr(lifespan, f"_{kind}_funcs") if f.__module__.startswith(PLUGIN_NAME)]


async def test_startup_and_shutdown_hooks_drive_the_scheduler():
    plugin = _plugin()
    scheduler = plugin.module._scheduler  # noqa: SLF001
    service = plugin.module._service  # noqa: SLF001
    original_groups = list(scheduler.config.stronghold_report_groups)

    lifespan = nonebot.get_driver()._lifespan  # noqa: SLF001
    startup = _hooks("startup")
    shutdown = _hooks("shutdown")
    assert startup and shutdown, "插件的启动/停机钩子必须注册在 driver 上"

    scheduler.config.stronghold_report_groups = [GROUP_ID]
    try:
        await lifespan._run_lifespan_func(startup)  # noqa: SLF001
        assert service.store._loaded is True  # noqa: SLF001 - 绑定表要在启动时读进来
        assert scheduler._task is not None  # noqa: SLF001
        assert not scheduler._task.done()

        # 刚起来不该立刻推（下一次是未来的 HH:MM）
        assert service.store.all() == []
    finally:
        scheduler.config.stronghold_report_groups = original_groups

    await lifespan._run_lifespan_func(shutdown)  # noqa: SLF001
    assert scheduler._task is None  # noqa: SLF001 - 停机后任务要收干净


def test_report_time_default_matches_the_requirement():
    plugin = _plugin()
    config = plugin.module._scheduler.config  # noqa: SLF001
    assert config.stronghold_report_time == "11:00", "日报默认 11:00"
    assert config.stronghold_report_tz == "Asia/Shanghai"
