"""卫戍协议：盟约 · QQ 机器人插件。

对接任意一台实现了 **Stronghold Bot Bridge** 契约（``docs/BRIDGE-API.md``）的卫戍协议服务端，
提供在线玩家、在线对局、战绩查询、账号绑定与每日 11 点日报。
"""

from __future__ import annotations

from nonebot import get_driver, get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from .config import Config
from .menu_data import MENU_DATA
from .runtime import command_prefix, set_service
from .scheduler import DailyReportScheduler
from .service import StrongholdService

__version__ = "0.1.0"

#: 指令前缀跟着部署的 COMMAND_START 走（含空串时用裸词）。
P = command_prefix()

USAGE = f"""\
{P}在线           在线玩家（含所在同盟与状态）
{P}对局           正在进行的对局
{P}战绩 [昵称]     查战绩：不填用绑定账号，填昵称按昵称查，@某人查 TA 的绑定账号
{P}绑定 <邮箱>     绑定游戏账号（验证码发到该账号邮箱）
{P}验证 <验证码>   完成绑定
{P}解绑           解除绑定
{P}我的账号        查看当前绑定
{P}日报 [日期]     今日（或指定日期）日报

日报每天 11:00 自动推送到配置的群（stronghold_report_groups）。
"""

__plugin_meta__ = PluginMetadata(
    name="卫戍协议",
    description="卫戍协议：盟约服务端伴侣：在线玩家 / 在线对局 / 战绩查询 / 账号绑定 / 日报",
    usage=USAGE,
    type="application",
    homepage="https://github.com/jinghuashang/nonebot-plugin-stronghold-protocol",
    config=Config,
    extra={
        "author": "jinghuashang",
        "version": __version__,
        # picmenu-next 的菜单声明：菜单模板见 menu/（模板名 stronghold）
        "pmn": {
            "hidden": False,
            "markdown": True,
            "template": "stronghold",
            "inherit_func_template": True,
            "alc_force_enable_detect": False,
        },
        "menu_data": MENU_DATA,
    },
)

driver = get_driver()
_config = get_plugin_config(Config)
_service = StrongholdService(_config)
_scheduler = DailyReportScheduler(_service, _config)
set_service(_service)


@driver.on_startup
async def _stronghold_startup() -> None:
    _service.store.load_sync()
    _scheduler.start()


@driver.on_shutdown
async def _stronghold_shutdown() -> None:
    await _scheduler.stop()


from . import commands  # noqa: E402,F401  —— 导入即注册命令（必须在 set_service 之后）

# 可选的图片菜单（nonebot-plugin-picmenu-next）：装了就注册 stronghold 模板与实时数据 mixin，
# 没装/加载失败就只是一条 debug 日志 —— 插件本体照常工作。
try:
    from . import menu  # noqa: E402,F401
except Exception as e:  # noqa: BLE001 - 菜单是可选功能，不能拖垮插件
    logger.warning(f"[stronghold] 图片菜单加载失败，已跳过：{type(e).__name__}: {e}")
