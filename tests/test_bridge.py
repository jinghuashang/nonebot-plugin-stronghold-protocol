"""BridgeClient：真实 HTTP 往返 + 错误码映射（契约里的每个错误码都要落到一个 BridgeError.code）。"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

import pytest

from nonebot_plugin_stronghold_protocol.bridge import (
    E_BAD_RESPONSE,
    E_NOT_IMPLEMENTED,
    E_UNAUTHORIZED,
    E_UNREACHABLE,
    BridgeClient,
    BridgeError,
)

TOKEN = "s3cret-token"


def client(bridge_server, token: str = TOKEN, **kwargs) -> BridgeClient:
    return BridgeClient(bridge_server.base_url, token=token, timeout=3.0, **kwargs)


async def test_meta_is_readable_and_parsed(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/meta", {
        "ok": True, "protocol": 1, "app": "0.2.6", "tz": "Asia/Shanghai", "accounts": True,
        "capabilities": ["status", "player", "daily", "bind.email"],
    })
    meta = await client(bridge_server, token="").meta()
    assert meta.protocol == 1
    assert meta.tz == "Asia/Shanghai"
    assert meta.has("bind.email")
    assert not meta.has("bind.operator")


async def test_token_and_protocol_headers_are_sent(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": True, "protocol": 1})
    await client(bridge_server).status()
    headers = bridge_server.last()["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["x-bot-token"] == TOKEN
    assert headers["x-bridge-protocol"] == "1"


async def test_query_parameters(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/player", {"ok": True, "player": {"playerId": "a_1", "name": "阿米娅"}})
    profile = await client(bridge_server).player(name="阿米娅")
    assert profile.player_id == "a_1"
    assert f"name={quote('阿米娅')}" in bridge_server.last()["path"]

    bridge_server.set("GET", "/api/bot/v1/daily", {"ok": True, "date": "2026-10-14"})
    assert (await client(bridge_server).daily(date(2026, 10, 14))).date == "2026-10-14"
    assert "date=2026-10-14" in bridge_server.last()["path"]


async def test_player_requires_an_argument(bridge_server):
    with pytest.raises(BridgeError) as err:
        await client(bridge_server).player()
    assert err.value.code == "bad_request"
    assert bridge_server.requests == []


async def test_bind_flow_posts_json(bridge_server):
    bridge_server.set("POST", "/api/bot/v1/bind/request",
                      {"ok": True, "requestId": "b_1", "maskedEmail": "3****@qq.com", "expiresAt": 1})
    challenge = await client(bridge_server).bind_request("10001@qq.com", qq="10001")
    assert challenge.request_id == "b_1"
    body = bridge_server.last()["body"]
    assert body == {"email": "10001@qq.com", "qq": "10001"}

    bridge_server.set("POST", "/api/bot/v1/bind/verify",
                      {"ok": True, "playerId": "a_0000000001", "name": "凯尔希", "email": "3****@qq.com"})
    result = await client(bridge_server).bind_verify("b_1", "123456", qq="10001")
    assert result.player_id == "a_0000000001"
    assert bridge_server.last()["body"] == {"requestId": "b_1", "code": "123456", "qq": "10001"}


# ---- 错误映射 -----------------------------------------------------------------------------------


async def test_401_is_unauthorized(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": False, "error": "unauthorized"}, status=401)
    with pytest.raises(BridgeError) as err:
        await client(bridge_server).status()
    assert err.value.code == E_UNAUTHORIZED
    assert err.value.status == 401


async def test_404_without_json_body_means_the_bridge_is_missing(bridge_server):
    # 没实现 Bridge 的服务端（例如原版上游）对未知路径回的是 HTML 404
    bridge_server.set("GET", "/api/bot/v1/daily", raw=b"<html>404</html>", status=404, content_type="text/html")
    with pytest.raises(BridgeError) as err:
        await client(bridge_server).daily()
    assert err.value.code == E_NOT_IMPLEMENTED


async def test_404_with_a_json_business_error_keeps_its_own_code(bridge_server):
    with pytest.raises(BridgeError) as err:
        await client(bridge_server, token="").daily()
    assert err.value.code == "not_found"


async def test_404_with_business_error_keeps_the_server_code(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/player", {"ok": False, "error": "player_not_found"}, status=404)
    with pytest.raises(BridgeError) as err:
        await client(bridge_server).player(name="查无此人")
    assert err.value.code == "player_not_found"


async def test_429_carries_the_server_message(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status",
                      {"ok": False, "error": "too_many_requests", "message": "慢点"}, status=429)
    with pytest.raises(BridgeError) as err:
        await client(bridge_server).status()
    assert err.value.code == "too_many_requests"
    assert err.value.message == "慢点"


async def test_env_proxy_is_ignored_by_default(bridge_server, monkeypatch):
    """环境里有 HTTP_PROXY 时，默认不能把游戏服的请求代理走（实测会被劫成 502）。"""
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": True, "protocol": 1})
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    status = await client(bridge_server).status()
    assert status.ok is True


async def test_ok_false_with_200(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", {"ok": False, "error": "unsupported"})
    with pytest.raises(BridgeError) as err:
        await client(bridge_server).status()
    assert err.value.code == "unsupported"


async def test_non_json_body(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/status", raw=b"<html>502</html>", content_type="text/html")
    with pytest.raises(BridgeError) as err:
        await client(bridge_server).status()
    assert err.value.code == E_BAD_RESPONSE


async def test_unreachable(closed_port):
    # 连一个没人监听的端口：Windows 上可能立刻 ECONNREFUSED，也可能等到超时，
    # 两种都属于「服务端不可达」，客户端必须报错而不是抛原始异常。
    with pytest.raises(BridgeError) as err:
        await BridgeClient(f"http://127.0.0.1:{closed_port}", token=TOKEN, timeout=2.0).status()
    assert err.value.code in (E_UNREACHABLE, "timeout")


async def test_missing_base_url():
    with pytest.raises(BridgeError) as err:
        await BridgeClient("", token=TOKEN).status()
    assert err.value.code == E_UNREACHABLE


async def test_timeout(bridge_server):
    bridge_server.set("GET", "/api/bot/v1/daily", {"ok": True}, delay=0.5)
    with pytest.raises(BridgeError) as err:
        await BridgeClient(bridge_server.base_url, token=TOKEN, timeout=0.05).daily()
    assert err.value.code == "timeout"
