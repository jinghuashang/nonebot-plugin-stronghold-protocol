"""图片菜单：模板注册、实时数据、混入行为。

真出图（跑浏览器）不放在单测里 —— 这里断言的是 HTML 与数据，起浏览器靠 docs/MENU.md 的手工步骤。
"""

from __future__ import annotations

import sys

import nonebot
import pytest

from nonebot_plugin_stronghold_protocol import runtime
from nonebot_plugin_stronghold_protocol.config import Config
from nonebot_plugin_stronghold_protocol.menu import available
from nonebot_plugin_stronghold_protocol.menu.live import LiveData, LiveSnapshot
from nonebot_plugin_stronghold_protocol.menu_data import MENU_DATA
from nonebot_plugin_stronghold_protocol.service import StrongholdService
from nonebot_plugin_stronghold_protocol.storage import BindingStore

pytestmark = pytest.mark.skipif(not available(), reason="未安装 nonebot-plugin-picmenu-next")

PLUGIN_ID = "nonebot_plugin_stronghold_protocol"


@pytest.fixture(scope="module", autouse=True)
def registered():
    """模板/mixin 现在是在 ``on_startup`` 注册的，测试里显式触发一次。"""
    from nonebot_plugin_stronghold_protocol.menu import register_menu_templates

    assert register_menu_templates() is True
    yield


@pytest.fixture
def service(tmp_path, bridge_server) -> StrongholdService:
    """指向假 Bridge 的服务对象（并临时替换全局单例）。"""
    previous = runtime.get_service()
    cfg = Config(stronghold_api_base=bridge_server.base_url, stronghold_api_token="t")
    svc = StrongholdService(cfg, store=BindingStore(tmp_path / "bindings.json"))
    runtime.set_service(svc)
    try:
        yield svc
    finally:
        runtime.set_service(previous)


@pytest.fixture
def menu_models():
    """两个假的 picmenu 菜单项：我们的插件 + 一个邻居。"""
    from nonebot_plugin_picmenu_next.data_source.models import PMDataItem, PMNPluginInfo

    ours = PMNPluginInfo(
        name="卫戍协议",
        plugin_id=PLUGIN_ID,
        author="jinghuashang",
        version="0.1.0",
        description="**在线 / 对局 / 战绩 / 日报** 一条龙。",
        usage="发送 `/帮助 卫戍协议 序号` 看功能详情。",
        pm_data=[PMDataItem(**item) for item in MENU_DATA],
        supported_adapters={"~onebot.v11"},
    )
    other = PMNPluginInfo(name="别的插件", plugin_id="nonebot_plugin_x", description="来做邻居的。")
    return ours, other


# ---- 模板注册 -----------------------------------------------------------------------------------


def test_stronghold_template_is_registered():
    from nonebot_plugin_picmenu_next.templates import detail_templates, func_detail_templates, index_templates

    for collector in (index_templates, detail_templates, func_detail_templates):
        assert "stronghold" in collector.data, "三个模板都要注册上"
        assert collector.get("stronghold") is collector.data["stronghold"]


def test_registration_is_deferred_to_startup_and_idempotent():
    """注册必须发生在 on_startup（导入期注册会让 bot 因重复加载 picmenu-next 崩溃重启）。"""
    import nonebot

    from nonebot_plugin_stronghold_protocol.menu import register_menu_templates

    lifespan = nonebot.get_driver()._lifespan  # noqa: SLF001
    hooks = [f for f in lifespan._startup_funcs  # noqa: SLF001
             if f.__module__.startswith("nonebot_plugin_stronghold_protocol.menu")]
    assert hooks, "启动钩子要注册在 driver 上"
    assert register_menu_templates() is True, "重复调用应当幂等返回 True"


def test_plugin_import_does_not_preload_picmenu_next():
    """回归测试：导入我们的插件**不能**把 picmenu-next 拉进来。

    线上事故：导入期 ``require("nonebot_plugin_picmenu_next")`` → bot 随后从 pyproject.toml
    再加载一次它 → `RuntimeError: Plugin already exists` → 容器重启循环。
    这里开一个干净子进程模拟 bot 的加载顺序，断言 picmenu-next 尚未被加载。
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    script = (
        "import sys, nonebot\n"
        "nonebot.init(driver='~fastapi', _env_file=None)\n"
        "nonebot.load_plugin('nonebot_plugin_stronghold_protocol')\n"
        "print('PICMENU_LOADED' if 'nonebot_plugin_picmenu_next' in sys.modules else 'PICMENU_NOT_LOADED')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(repo_root), "LOCALSTORE_USE_CWD": "true"},
        cwd=str(repo_root),
    )
    assert "PICMENU_NOT_LOADED" in proc.stdout, f"导入期就把 picmenu-next 拉进来了：{proc.stdout}\n{proc.stderr[-800:]}"


def test_mixin_is_registered_globally_with_our_own_guard():
    """mixin 必须登记在**全局**收集器里（self_* 在 on_startup 注册会挂到 picmenu-next 名下）。"""
    from nonebot_plugin_picmenu_next.data_source.mixin import plugin_detail_mixins, self_detail_mixins

    from nonebot_plugin_stronghold_protocol.menu import mixins

    funcs = [m.func for m in plugin_detail_mixins.data]
    assert mixins._inject_live in funcs
    assert "nonebot_plugin_stronghold_protocol" not in dict(self_detail_mixins), "不要用 self_detail_mixins"
    assert mixins.PLUGIN_ID == "nonebot_plugin_stronghold_protocol"


def test_duplicate_registration_only_warns():
    """重名不能抛 ValueError（cookit 的收集器默认会抛）。"""
    from nonebot_plugin_picmenu_next.templates import index_templates

    from nonebot_plugin_stronghold_protocol.menu.template import _register

    calls = []

    def handler(*_args):
        calls.append(1)

    assert _register(index_templates, "stronghold", handler) is False
    assert calls == []
    assert _register(index_templates, "stronghold-test-new", handler) is True
    assert calls == []
    assert _register(index_templates, "stronghold-test-new", handler) is False  # 第二次跳过
    index_templates.data.pop("stronghold-test-new", None)


def test_metadata_declares_the_menu():
    plugin = nonebot.get_plugin_by_module_name(PLUGIN_ID)
    assert plugin is not None
    extra = plugin.metadata.extra
    assert extra["pmn"]["template"] == "stronghold"
    assert extra["pmn"]["markdown"] is True
    assert len(extra["menu_data"]) == 8
    for item in extra["menu_data"]:
        assert set(item) >= {"func", "trigger_method", "trigger_condition", "brief_des", "detail_des"}


# ---- 视图数据 -----------------------------------------------------------------------------------


def test_index_view_counts_and_groups(menu_models):
    from nonebot_plugin_stronghold_protocol.menu.template import build_index_view

    ours, other = menu_models
    view = build_index_view([ours, other], showing_hidden=False)
    assert view["plugin_count"] == 2
    assert view["func_count"] == 8
    assert [g["title"] for g in view["groups"]] == ["应用"]
    assert [c["no"] for g in view["groups"] for c in g["cards"]] == [1, 2]
    assert view["groups"][0]["cards"][0]["is_ours"] is True
    assert view["hidden_count"] == 0  # 没显示隐藏项时不统计


def test_detail_view_lists_functions(menu_models):
    from nonebot_plugin_stronghold_protocol.menu.template import build_detail_view

    ours, _ = menu_models
    view = build_detail_view(ours)
    assert view["func_count"] == 8
    assert view["is_ours"] is True
    assert view["plugin_type"] == "application"
    assert view["adapters"] == ["~onebot.v11"]
    assert [e["no"] for e in view["func_entries"]] == list(range(1, 9))
    assert view["func_entries"][0]["func"].func == "在线玩家"


# ---- HTML 渲染（不起浏览器）---------------------------------------------------------------------


async def test_index_html_has_the_extra_blocks(menu_models, service):
    from nonebot_plugin_stronghold_protocol.menu import template

    ours, other = menu_models
    live = LiveSnapshot(online=12, in_match=5, waiting=4, has_daily=True, date="2026-10-15",
                        today_matches=12, today_victories=8, today_win_rate=2 / 3, today_gold=12345, today_leaks=37)
    html = await template.render_html(
        "index.html.jinja",
        infos=[ours, other],
        showing_hidden=True,
        user_can_see_hidden=True,
        live=live,
        server_name="卫戍协议：盟约",
        now_text="2026-10-15 11:00",
        **template.build_index_view([ours, other], True),
    )
    # 比默认多出来的部分
    assert "hero-stats" in html
    assert "live-bar" in html
    assert "STRONGHOLD PROTOCOL" in html          # 技术微标
    assert "在线" in html and "12 人" in html      # HUD 格子（label 与数值分开渲染）
    assert "消耗资金" in html and "12,345" in html
    assert 'class="gold"' in html                  # 金额用金色
    assert "card--ours" in html
    assert "生成于 2026-10-15 11:00" in html
    assert "nonebot-plugin-stronghold-protocol v" in html   # 页脚：插件名 + 版本
    assert "PicMenu-Next" not in html and "模板 stronghold" not in html
    # 邻居插件照样在
    assert "别的插件" in html


async def test_index_html_without_live_data(menu_models, service):
    from nonebot_plugin_stronghold_protocol.menu import template

    ours, _ = menu_models
    html = await template.render_html(
        "index.html.jinja", infos=[ours], showing_hidden=False,
        user_can_see_hidden=None, live=None, server_name="卫戍协议：盟约", now_text="2026-10-15 11:00",
        **template.build_index_view([ours], False),
    )
    assert "实时数据不可用" in html
    assert "live-bar--off" in html


async def test_detail_html_has_table_and_quick_start(menu_models, service):
    from nonebot_plugin_stronghold_protocol.menu import template

    ours, _ = menu_models
    html = await template.render_html(
        "detail.html.jinja", info=ours, info_index=0, showing_hidden=False, user_can_see_hidden=None,
        live=None, server_name="卫戍协议：盟约", now_text="2026-10-15 11:00", **template.build_detail_view(ours),
    )
    assert "func-table" in html
    assert "在线玩家" in html and "在线对局" in html and "日报定时推送" in html
    assert "快速上手" in html            # 我们自己的插件才有
    assert "jinghuashang" in html        # 作者 chips
    assert "~onebot.v11" in html         # 适配器 chips
    assert "No.1" in html


async def test_func_detail_html(menu_models, service):
    from nonebot_plugin_stronghold_protocol.menu import template

    ours, _ = menu_models
    func = ours.pm_data[1]
    html = await template.render_html(
        "func_detail.html.jinja", info=ours, info_index=0, func_index=1, showing_hidden=False,
        user_can_see_hidden=None, server_name="卫戍协议：盟约", now_text="2026-10-15 11:00",
        **template.build_detail_view(ours, func=func),
    )
    assert "在线对局" in html
    assert "触发方式" in html and "触发条件" in html and "详细用法" in html
    assert "回到" in html


# ---- 实时数据层 ---------------------------------------------------------------------------------


async def test_live_snapshot_reads_bridge(service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {
        "ok": True, "protocol": 1,
        "counts": {"online": 3, "inMatch": 2, "waiting": 1, "inHall": 1, "rooms": 2, "matches": 2},
    })
    bridge_server.set("GET", "/api/bot/v1/daily", {
        "ok": True, "protocol": 1, "date": "2026-10-15",
        "summary": {"matches": 4, "finished": 4, "victories": 3, "defeats": 1, "winRate": 0.75,
                    "players": 2, "goldSpent": 999, "leaks": 5},
        "titles": {
            "comment_1": {"holders": [{"playerId": "a_1", "name": "阿米娅", "count": 2, "bestRounds": 15}]},
            "worst": {"kind": "leaks", "holders": [{"playerId": "a_2", "name": "玫兰莎", "leaks": 12, "best": 7}]},
        },
    })
    live = LiveData(service, ttl=30, timeout=2)
    snap = await live.snapshot()
    assert snap is not None
    assert (snap.online, snap.in_match, snap.in_hall) == (3, 2, 1)
    assert snap.has_daily is True
    assert snap.today_matches == 4 and snap.today_gold == 999
    assert snap.today_win_rate == pytest.approx(0.75)
    assert snap.titles["comment_1"] == "阿米娅 ×2（最远 15 回合）"
    assert snap.titles["worst"] == "玫兰莎 12 只"
    assert "在线 3 人" in snap.online_line and "消耗资金 999" in snap.daily_line


async def test_live_snapshot_is_cached_and_single_flight(service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": True, "counts": {"online": 1}})
    bridge_server.set("GET", "/api/bot/v1/daily", {"ok": True, "summary": {"matches": 0}})
    live = LiveData(service, ttl=30, timeout=2)
    import asyncio

    await asyncio.gather(*(live.snapshot() for _ in range(5)))
    status_calls = [r for r in bridge_server.requests if r["path"].startswith("/api/bot/v1/status")]
    assert len(status_calls) == 1, "并发只打一次"

    await live.snapshot()
    assert len([r for r in bridge_server.requests if r["path"].startswith("/api/bot/v1/status")]) == 1, "TTL 内命中缓存"

    live.invalidate()
    await live.snapshot()
    assert len([r for r in bridge_server.requests if r["path"].startswith("/api/bot/v1/status")]) == 2


async def test_live_snapshot_degrades_when_bridge_is_down(service, closed_port, bridge_server):
    service.client.base_url = f"http://127.0.0.1:{closed_port}"
    live = LiveData(service, ttl=30, timeout=1, failure_ttl=5)
    assert await live.snapshot() is None, "连不上要返回 None，而不是抛错"


async def test_live_snapshot_degrades_when_bridge_not_implemented(service, bridge_server):
    live = LiveData(service, ttl=30, timeout=1)
    assert await live.snapshot() is None  # 假服务端对未注册路径回 404


async def test_live_snapshot_survives_missing_daily(service, bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": True, "counts": {"online": 2}})
    bridge_server.set("GET", "/api/bot/v1/daily", {"ok": False, "error": "unsupported"}, status=200)
    live = LiveData(service, ttl=30, timeout=2)
    snap = await live.snapshot()
    assert snap is not None and snap.online == 2
    assert snap.has_daily is False and snap.daily_line == ""


# ---- mixin --------------------------------------------------------------------------------------


async def test_self_mixin_injects_live_block_without_polluting(service, bridge_server, menu_models):
    from nonebot_plugin_picmenu_next.data_source.mixin import resolve_detail_mixin

    bridge_server.set("GET", "/api/bot/v1/status",
                      {"ok": True, "counts": {"online": 7, "inMatch": 3}})
    bridge_server.set("GET", "/api/bot/v1/daily", {
        "ok": True, "date": "2026-10-15",
        "summary": {"matches": 2, "victories": 1, "winRate": 0.5, "goldSpent": 88, "leaks": 1},
        "titles": {"comment_1": {"holders": [{"playerId": "a_1", "name": "阿米娅", "count": 1}]}},
    })
    from nonebot_plugin_stronghold_protocol.menu import live as live_module

    live_module.reset_live()
    ours, other = menu_models
    original = ours.description

    merged = await resolve_detail_mixin(ours)
    assert merged is not ours, "必须返回新对象，不能原地改"
    assert ours.description == original, "原对象不能被污染"
    assert "服务器实时状态" in merged.description
    assert "在线 7 人" in merged.description
    assert "今日战报（2026-10-15）" in merged.description
    assert "卫戍之星：阿米娅 ×1" in merged.description

    # 别人的插件不受影响
    neighbor = await resolve_detail_mixin(other)
    assert "服务器实时状态" not in (neighbor.description or "")
    live_module.reset_live()


async def test_self_mixin_skips_when_bridge_is_down(service, closed_port, menu_models):
    from nonebot_plugin_picmenu_next.data_source.mixin import resolve_detail_mixin

    from nonebot_plugin_stronghold_protocol.menu import live as live_module

    service.client.base_url = f"http://127.0.0.1:{closed_port}"
    live_module.reset_live()
    ours, _ = menu_models
    original = ours.description
    merged = await resolve_detail_mixin(ours)
    assert merged.description == original, "取数失败就不注入，而不是写「不可用」"
    live_module.reset_live()


async def test_self_mixin_respects_the_switch(service, bridge_server, menu_models):
    from nonebot_plugin_picmenu_next.data_source.mixin import resolve_detail_mixin

    from nonebot_plugin_stronghold_protocol.menu import live as live_module

    service.config.stronghold_menu_live = False
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": True, "counts": {"online": 99}})
    live_module.reset_live()
    ours, _ = menu_models
    merged = await resolve_detail_mixin(ours)
    assert "服务器实时状态" not in (merged.description or "")
    assert bridge_server.requests == [], "关掉开关就完全不请求服务端"
    service.config.stronghold_menu_live = True
    live_module.reset_live()


def test_module_reports_python_requirement():
    from nonebot_plugin_stronghold_protocol.menu import PMN_MIN_PYTHON

    assert PMN_MIN_PYTHON == (3, 10)
    assert available() is (sys.version_info >= PMN_MIN_PYTHON)


# ---- 指令前缀（菜单与提示都要跟着部署的 COMMAND_START 走）------------------------------------


def test_command_prefix_follows_command_start(monkeypatch):
    import nonebot

    from nonebot_plugin_stronghold_protocol.runtime import command_prefix

    config = nonebot.get_driver().config
    original = config.command_start
    try:
        config.command_start = {"/"}
        assert command_prefix() == "/"
        config.command_start = {"", "!", "#"}   # 线上那台就是这种：用户直接敲「在线」
        assert command_prefix() == ""
        config.command_start = {"!", "#"}
        assert command_prefix() in {"!", "#"}
        config.command_start = set()
        assert command_prefix() == "/"
    finally:
        config.command_start = original


def test_footer_shows_our_plugin_name_and_version():
    from nonebot_plugin_stronghold_protocol import __version__
    from nonebot_plugin_stronghold_protocol.menu.template import PLUGIN_DIST, _footer_context

    ctx = _footer_context()
    assert ctx == {"plugin_name": PLUGIN_DIST, "plugin_version": __version__}
    assert PLUGIN_DIST == "nonebot-plugin-stronghold-protocol"
    assert __version__ and __version__ != "0.0.0"


def test_menu_data_uses_the_configured_prefix(monkeypatch):
    """菜单文案里的命令必须真的能敲出来（前缀变了文案就跟着变）。"""
    import importlib

    import nonebot

    config = nonebot.get_driver().config
    original = config.command_start
    try:
        config.command_start = {""}
        import nonebot_plugin_stronghold_protocol.menu_data as menu_data

        reloaded = importlib.reload(menu_data)
        by_func = {item["func"]: item for item in reloaded.MENU_DATA}
        assert by_func["在线玩家"]["trigger_method"].startswith("`在线`")
        assert "/在线" not in by_func["在线玩家"]["trigger_method"]
        assert by_func["绑定游戏账号"]["trigger_method"] == "`绑定 <游戏账号邮箱>`"

        config.command_start = {"/"}
        reloaded = importlib.reload(menu_data)
        by_func = {item["func"]: item for item in reloaded.MENU_DATA}
        assert by_func["在线玩家"]["trigger_method"] == "`/在线`（别名 `/在线玩家` `/online`）"
    finally:
        config.command_start = original
        importlib.reload(importlib.import_module("nonebot_plugin_stronghold_protocol.menu_data"))
