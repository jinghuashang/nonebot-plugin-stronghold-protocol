# Temporary one-shot: send the upcoming-update announcement as the plugin's card/menu image.
import asyncio
from pathlib import Path

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from src.plugins.nonebot_plugin_stronghold_protocol.cards import build_image
from src.plugins.nonebot_plugin_stronghold_protocol.cards.views import note_view

GROUP = 1128024089
MARK = Path('/app/data/stronghold-announce-sent')
TEXT = """服务器即将进行更新，更新时需要短暂重启，具体时间会再通知；请提前结束对局，避免损失。
本次更新将把联防结算改为“最终击杀”判定：最后一击的干员获得悬赏资金与击杀结算；没有玩家最终击杀时不再按地块或半场补偿。
蓝门/红门资源缺失的玩家，更新后请重新导入最新素材包。
重启前服务器继续正常运行。"""


driver = get_driver()


@driver.on_bot_connect
async def _send_once(bot):
    if MARK.exists():
        return
    try:
        await asyncio.sleep(2)
        image = await build_image('note', {
            **note_view('服务器即将更新', TEXT, 'ok'),
            'crumb': '卫戍协议 / 更新公告',
        })
        if image:
            await bot.send_group_msg(group_id=GROUP, message=Message(MessageSegment.image(image)))
        else:
            await bot.send_group_msg(group_id=GROUP, message=TEXT)
        MARK.write_text('sent', encoding='utf-8')
        logger.info('[stronghold-announce] sent')
    except Exception as e:
        logger.error(f'[stronghold-announce] send failed: {e!r}')
