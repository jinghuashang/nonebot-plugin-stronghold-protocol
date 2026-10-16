"""测试夹具：一个真实的本地 HTTP 服务器（不是 mock），把 BridgeClient 整条链路跑通。"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import nonebot
import pytest

# picmenu-next 的依赖 cookit 会拒绝「localstore 用默认用户目录」的配置（见 docs/MENU.md），
# 测试里让它把数据放到当前目录，省得每次都要人肉加配置。
os.environ.setdefault("LOCALSTORE_USE_CWD", "true")

# 插件包的 __init__.py 会在导入时注册 driver 钩子，所以得先 init（NoneBot 正常启动顺序也是这样）。
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", _env_file=None)

# 像生产那样由 NoneBot 加载插件：PluginMetadata、matcher 注册、启动钩子都走真实路径。
nonebot.load_plugin("nonebot_plugin_stronghold_protocol")


class FakeBridge:
    """可编程的假服务端：按 ``(method, path)`` 注册响应，并记下收到的每个请求。"""

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Tuple[int, bytes, str]] = {}
        self.delays: Dict[Tuple[str, str], float] = {}
        self.requests: List[Dict[str, Any]] = []
        self.base_url = ""

    def set(self, method: str, path: str, body: Any = None, *, status: int = 200, raw: Optional[bytes] = None,
            content_type: str = "application/json; charset=utf-8", delay: float = 0.0) -> None:
        payload = raw if raw is not None else json.dumps(body if body is not None else {"ok": True}).encode("utf-8")
        self.routes[(method.upper(), path)] = (status, payload, content_type)
        self.delays[(method.upper(), path)] = delay

    def last(self) -> Dict[str, Any]:
        return self.requests[-1]


def _make_handler(fake: FakeBridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # 测试里不要噪音
            pass

        def _serve(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body: Any
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                body = raw
            fake.requests.append({
                "method": method,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            })
            path = self.path.split("?", 1)[0]
            hit = fake.routes.get((method, path)) or fake.routes.get((method, "*"))
            if hit is None:
                status, payload, ctype = 404, b'{"ok":false,"error":"not_found"}', "application/json"
            else:
                status, payload, ctype = hit
            delay = fake.delays.get((method, path), 0.0)
            if delay:
                import time

                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Bridge-Protocol", "1")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server 的约定
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._serve("DELETE")

    return Handler


@pytest.fixture
def bridge_server():
    fake = FakeBridge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def closed_port() -> int:
    """一个刚被关掉的端口 —— 用来验证「连不上」的分支。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    return port
