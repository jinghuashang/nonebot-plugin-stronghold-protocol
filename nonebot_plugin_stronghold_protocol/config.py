"""插件配置：全部字段以 STRONGHOLD_ 开头，写在 NoneBot 的 .env 里。"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    """NoneBot 会把 .env 里 ``STRONGHOLD_*`` 的键注入这里（键名相同）。"""

    # ---- 卫戍协议服务端（Bridge 接入，见 docs/BRIDGE-API.md）------------------------------------
    #: 游戏服地址。裸核心默认 3000；反向代理/容器入口按实际端口填。
    stronghold_api_base: str = "http://127.0.0.1:3000"
    #: Bridge 接口前缀。服务端可以把契约挂到别处，改这里即可。
    stronghold_api_prefix: str = "/api/bot/v1"
    #: Bridge 令牌（服务端 ``BOT_TOKEN``）。留空时按匿名访问，只有公开接口能用。
    stronghold_api_token: str = ""
    #: 单次请求超时（秒）。
    stronghold_timeout: float = 8.0
    #: HTTPS 自签证书时才需要关掉。
    stronghold_verify_tls: bool = True
    #: 是否让请求走 HTTP_PROXY / ALL_PROXY 等环境变量。默认关：游戏服一般在本机或内网，
    #: 被环境里的代理劫走会直接报 502（实测踩过）。
    stronghold_use_env_proxy: bool = False

    # ---- 展示 ------------------------------------------------------------------------------------
    #: /在线 最多显示多少名玩家（服务端返回的顺序即展示顺序）。
    stronghold_online_limit: int = 20
    #: /对局 最多显示多少局。
    stronghold_match_limit: int = 10
    #: /战绩 最多显示多少条最近对局。
    stronghold_profile_matches: int = 5

    # ---- 账号绑定 --------------------------------------------------------------------------------
    #: 待验证绑定（已发码、等玩家回码）的本地有效期，分钟。
    stronghold_bind_ttl_min: int = 10
    #: 绑定表存放目录；留空 = ``<cwd>/data/nonebot_plugin_stronghold_protocol``。
    stronghold_data_dir: str = ""

    # ---- 日报 ------------------------------------------------------------------------------------
    #: 是否开启每日定时推送。
    stronghold_report_enabled: bool = True
    #: 推送时刻，24 小时制 HH:MM（按 stronghold_report_tz）。
    stronghold_report_time: str = "11:00"
    #: 日报时区：既是推送时刻的时区，也是「今日」的日界。
    stronghold_report_tz: str = "Asia/Shanghai"
    #: 命令回复与日报推送出图（需要 nonebot-plugin-htmlrender；渲染失败会自动回退文本）。
    stronghold_reply_image: bool = True
    #: 推送目标群号列表；空列表 = 只提供 /日报 手动查询，不自动推送。
    stronghold_report_groups: List[int] = Field(default_factory=list)
    #: 日报里每个称号最多列几人，0 = 全部。
    stronghold_report_max_titles: int = 5

    # ---- 图片菜单（可选：装了 nonebot-plugin-picmenu-next 才生效）-------------------------------
    #: 菜单里显示的服务器名（首页标题、页脚）。
    stronghold_menu_server_name: str = "卫戍协议：盟约"
    #: 是否在菜单里显示服务器实时状态（在线人数 / 今日战报）。关掉就完全不请求服务端。
    stronghold_menu_live: bool = True
    #: 实时状态缓存秒数。菜单可能被频繁打开，太短会把服务端打满。
    stronghold_menu_live_ttl: float = 30.0
    #: 菜单里取实时数据的超时（秒）；超时就只显示「实时数据不可用」。
    stronghold_menu_timeout: float = 2.0

    model_config = ConfigDict(extra="ignore")
