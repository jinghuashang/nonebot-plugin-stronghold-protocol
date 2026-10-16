"""卫戍协议 Bot Bridge HTTP 客户端（契约见 docs/BRIDGE-API.md）。

插件只通过这一个客户端取数：服务端（上游或任何 fork）实现契约后，任何人的服务器都能接进来。
客户端不做缓存、不做重试 —— 命令都是人手动触发，一次请求一次响应最可预测。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional, Tuple

import httpx

from .models import (
    BindChallenge,
    BindResult,
    DailyReport,
    PlayerProfile,
    ServerMeta,
    ServerStatus,
)

#: 契约版本：请求头 ``X-Bridge-Protocol`` 带上它，服务端可以据此兼容。
PROTOCOL_VERSION = 1

#: 客户端自己产生的错误码（服务端错误码原样透传）。
E_UNREACHABLE = "unreachable"
E_TIMEOUT = "timeout"
E_UNAUTHORIZED = "unauthorized"
E_NOT_IMPLEMENTED = "not_implemented"
E_RATE_LIMITED = "rate_limited"
E_BAD_RESPONSE = "bad_response"
E_HTTP_ERROR = "http_error"


class BridgeError(RuntimeError):
    """一次 Bridge 调用失败。

    :param code: 机器可读错误码（服务端 ``error`` 字段，或上面的 ``E_*``）
    :param message: 服务端给的中文说明（可能为空）
    :param status: HTTP 状态码（网络层错误为 None）
    :param detail: 服务端附带的细节字符串
    """

    def __init__(self, code: str, message: str = "", status: Optional[int] = None, detail: Optional[str] = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


def _first_message(body: Any) -> Tuple[str, Optional[str]]:
    """从服务端的错误体里取 (message, detail)。"""
    if not isinstance(body, dict):
        return "", None
    msg = body.get("message")
    detail = body.get("detail")
    return (msg if isinstance(msg, str) else "",
            detail if isinstance(detail, str) else (None if detail is None else str(detail)))


class BridgeClient:
    """``/api/bot/v1`` 的异步客户端。

    :param base_url: 游戏服地址，例如 ``http://127.0.0.1:3000``
    :param token: Bridge 令牌；空字符串表示匿名（只能读公开能力）
    :param prefix: 契约前缀，默认 ``/api/bot/v1``
    :param timeout: 单次请求超时（秒）
    :param verify_tls: HTTPS 校验开关
    :param use_env_proxy: 是否读 ``HTTP_PROXY`` 等环境变量。默认 **不读**：游戏服通常是
        本机/内网地址，环境里的代理会把它劫走（实测会直接变成 502）。
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        prefix: str = "/api/bot/v1",
        timeout: float = 8.0,
        verify_tls: bool = True,
        use_env_proxy: bool = False,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.prefix = "/" + (prefix or "/api/bot/v1").strip().strip("/")
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.use_env_proxy = use_env_proxy

    # ---- 低层 ------------------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{self.prefix}{path}"

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Bridge-Protocol": str(PROTOCOL_VERSION),
            "User-Agent": "nonebot-plugin-stronghold-protocol",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            # 服务端可以只认这一种；两个都带，谁都认得出。
            headers["X-Bot-Token"] = self.token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.base_url:
            raise BridgeError(E_UNREACHABLE, "未配置 stronghold_api_base")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                verify=self.verify_tls,
                follow_redirects=False,
                trust_env=self.use_env_proxy,
            ) as client:
                resp = await client.request(
                    method,
                    self._url(path),
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as e:
            raise BridgeError(E_TIMEOUT, f"请求超时（{self.timeout:g}s）", detail=str(e)) from e
        except httpx.HTTPError as e:
            raise BridgeError(E_UNREACHABLE, "连不上卫戍协议服务端", detail=str(e)) from e

        status = resp.status_code
        try:
            body: Any = resp.json()
        except ValueError:
            body = None

        # 服务端的业务错误（ok=false）优先：它的 error 码比 HTTP 状态更能说明问题
        # —— 例如 /player 找不到玩家是 404 + player_not_found，而「这个接口没实现」也是 404。
        if isinstance(body, dict) and body.get("ok") is False and isinstance(body.get("error"), str) and body["error"]:
            msg, detail = _first_message(body)
            raise BridgeError(body["error"], msg, status=status, detail=detail)

        if status == 401 or status == 403:
            msg, detail = _first_message(body)
            raise BridgeError(E_UNAUTHORIZED, msg or "Bridge 令牌无效或未配置", status=status, detail=detail)
        if status == 404:
            raise BridgeError(E_NOT_IMPLEMENTED, "服务端尚未实现该 Bridge 接口", status=status)
        if status == 405:
            raise BridgeError("method_not_allowed", "服务端不接受该请求方法", status=status)
        if status == 429:
            msg, detail = _first_message(body)
            raise BridgeError(E_RATE_LIMITED, msg or "请求过于频繁，请稍后再试", status=status, detail=detail)
        if status >= 500:
            msg, detail = _first_message(body)
            raise BridgeError("service_unavailable", msg or f"服务端错误（HTTP {status}）",
                              status=status, detail=detail)
        if body is None:
            raise BridgeError(E_BAD_RESPONSE, "服务端返回的不是 JSON", status=status)
        if status != 200:
            msg, detail = _first_message(body)
            raise BridgeError(E_HTTP_ERROR, msg or f"服务端返回 HTTP {status}", status=status, detail=detail)
        if not isinstance(body, dict):
            raise BridgeError(E_BAD_RESPONSE, "服务端返回的 JSON 不是对象")
        return body

    # ---- 契约方法 --------------------------------------------------------------------------------

    async def meta(self) -> ServerMeta:
        """``GET /meta``：服务端版本与能力清单。"""
        return ServerMeta.from_json(await self._request("GET", "/meta"))

    async def status(self) -> ServerStatus:
        """``GET /status``：在线玩家 + 在线对局 + 计数。"""
        return ServerStatus.from_json(await self._request("GET", "/status"))

    async def player(self, *, player_id: Optional[str] = None, name: Optional[str] = None) -> PlayerProfile:
        """``GET /player``：按 ``playerId``（优先）或昵称查战绩。"""
        params: Dict[str, Any] = {}
        if player_id:
            params["playerId"] = player_id
        elif name:
            params["name"] = name
        else:
            raise BridgeError("bad_request", "playerId 与 name 至少要给一个")
        return PlayerProfile.from_json(await self._request("GET", "/player", params=params))

    async def daily(self, day: Optional[date] = None) -> DailyReport:
        """``GET /daily``：某一天（默认服务端的「今天」）的日报数据。"""
        params = {"date": day.isoformat()} if day else None
        return DailyReport.from_json(await self._request("GET", "/daily", params=params))

    async def bind_request(self, email: str, qq: str = "") -> BindChallenge:
        """``POST /bind/request``：把验证码发到游戏账号邮箱。"""
        body: Dict[str, Any] = {"email": email}
        if qq:
            body["qq"] = qq
        return BindChallenge.from_json(await self._request("POST", "/bind/request", json_body=body))

    async def bind_verify(self, request_id: str, code: str, qq: str = "") -> BindResult:
        """``POST /bind/verify``：校验验证码，成功则得到账号的 playerId。"""
        body: Dict[str, Any] = {"requestId": request_id, "code": code}
        if qq:
            body["qq"] = qq
        return BindResult.from_json(await self._request("POST", "/bind/verify", json_body=body))
