"""菜单内容：`PluginMetadata.extra["menu_data"]` 用的三级菜单项。

放在独立模块里，是为了让「菜单文案」与「菜单渲染」分开：改文案不用碰 picmenu-next 相关代码。

触发器前缀按 NoneBot 的 `COMMAND_START` 自动取：有空前缀（`""`）就用裸词（部署里很常见，
用户直接敲 `在线`），否则用第一个非空前缀（如 `/`）。这样菜单里写的命令**一定是能敲出来的**。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .runtime import command_prefix

_P = command_prefix()


def _method(primary: str, aliases: tuple = ()) -> str:
    """触发方式文案：``/在线``（别名 ``/在线玩家`` ``/online``）。"""
    text = f"`{_P}{primary}`"
    if aliases:
        joined = " ".join(f"`{_P}{a}`" for a in aliases)
        text += f"（别名 {joined}）"
    return text


#: picmenu-next 的三级菜单项（每项：func / trigger_method / trigger_condition / brief_des / detail_des）。
MENU_DATA: List[Dict[str, Any]] = [
    {
        "func": "在线玩家",
        "trigger_method": _method("在线", ("在线玩家", "online")),
        "trigger_condition": "任何时候都可以查",
        "brief_des": "看当前在线博士、状态与所在同盟",
        "detail_des": (
            "列出在线人数与状态（对局中 / 待开局 / 大厅 / 空闲）、同盟密钥、难度、是否房主、观战；"
            "历史记录与「观战」名单也在这一条里。\n\n"
            "显示条数可用 `stronghold_online_limit` 调整（默认 20）。"
        ),
    },
    {
        "func": "在线对局",
        "trigger_method": _method("对局", ("在线对局", "matches")),
        "trigger_condition": "任何时候都可以查",
        "brief_des": "看正在进行中的对局",
        "detail_des": (
            "按房间列出模式（独立 / 同盟）、难度、回合进度、参战博士与 AI 数量、开局时间。"
            "显示条数可用 `stronghold_match_limit` 调整（默认 10）。"
        ),
    },
    {
        "func": "战绩查询",
        "trigger_method": (
            f"`{_P}战绩 [昵称]`（别名 `{_P}查询战绩`；也可 `{_P}战绩 @某人`）"
        ),
        "trigger_condition": "不填昵称时需要有绑定账号",
        "brief_des": "今日与累计战绩、称号次数、最近对局",
        "detail_des": (
            "不填昵称 → 用你自己的绑定账号；填昵称 → 按昵称查；`@某人` → 查 TA 绑定账号的战绩。\n\n"
            "显示：局数 / 胜率 / 消耗资金 / 漏怪 / 最远回合、五个称号的获得次数、最近若干局"
            "（条数见 `stronghold_profile_matches`）。"
        ),
    },
    {
        "func": "绑定游戏账号",
        "trigger_method": f"`{_P}绑定 <游戏账号邮箱>`",
        "trigger_condition": "服务端开启了账号系统（`bind.email` 能力）",
        "brief_des": "把 QQ 和游戏账号绑起来",
        "detail_des": (
            f"验证码会发到该游戏账号的邮箱，回复 `{_P}验证 <验证码>` 完成绑定。\n\n"
            "绑定关系只存在机器人这一侧；服务端只负责证明「这个邮箱归你」——它不会保存你的 QQ 号。"
            "同一个游戏账号被别的 QQ 绑走时，原绑定会自动解除。"
        ),
    },
    {
        "func": "验证绑定",
        "trigger_method": _method("验证", ("绑定验证",)) ,
        "trigger_condition": f"刚发过 `{_P}绑定` 且没超时",
        "brief_des": "完成绑定",
        "detail_des": (
            "验证码有效期由 `stronghold_bind_ttl_min` 决定（默认 10 分钟）。打错了可以直接重试；"
            f"服务端判定失效后要重新 `{_P}绑定` 发码。"
        ),
    },
    {
        "func": "解绑 / 我的账号",
        "trigger_method": f"`{_P}解绑` · `{_P}我的账号`（别名 `{_P}我的绑定` `{_P}whoami`）",
        "trigger_condition": "已绑定",
        "brief_des": "查看当前绑定或解除绑定",
        "detail_des": (
            f"`{_P}我的账号` 显示昵称、playerId、邮箱与绑定时间；`{_P}解绑` 删除绑定，"
            "**不影响游戏账号本身**。"
        ),
    },
    {
        "func": "今日日报",
        "trigger_method": _method("日报", ("今日日报", "daily")) + "（可带日期，如 `10-15`、`昨天`）",
        "trigger_condition": "任何时候都可以查，历史日期也可以",
        "brief_des": "当日七项指标：五个称号 + 胜率 + 消耗资金",
        "detail_des": (
            "日期可写 `2026-10-15`、`10-15` 或 `昨天`。\n\n"
            "统计项：卫戍之星、坚若磐石、挥金如土、精英云集、卫戍之区、今日胜率、今日消耗资金"
            "（训练局不计入胜率）。每个称号最多列 `stronghold_report_max_titles` 人。"
        ),
    },
    {
        "func": "日报定时推送",
        "trigger_method": "自动：每天 `stronghold_report_time`（默认 11:00）",
        "trigger_condition": "配置了 `stronghold_report_groups`",
        "brief_des": "每天 11 点把日报发到群里",
        "detail_des": (
            "时区由 `stronghold_report_tz` 决定（默认 Asia/Shanghai），日界也用这个时区。"
            "关掉推送把 `stronghold_report_enabled` 设为 false 即可（`/日报` 手动查询不受影响）。"
        ),
    },
]
