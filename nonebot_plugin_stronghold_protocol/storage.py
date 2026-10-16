"""本地存储：QQ ↔ 游戏账号（playerId）绑定表 + 待验证的绑定请求。

只放在机器人这一侧：服务端只负责「证明这个邮箱归这个人」（发码/校验），绑定关系由插件保管，
所以换游戏服、服务端不支持绑定能力都不影响已记录的绑定（只是无法再新增）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

BINDING_FILE_VERSION = 1


@dataclass
class Binding:
    """一个 QQ 号绑定的游戏账号。"""

    qq: str
    player_id: str
    name: str = ""
    email: str = ""
    bound_at: int = 0

    @staticmethod
    def from_json(v: object) -> Optional["Binding"]:
        if not isinstance(v, dict):
            return None
        qq = v.get("qq")
        player_id = v.get("playerId")
        if not isinstance(qq, str) or not isinstance(player_id, str) or not qq or not player_id:
            return None
        return Binding(
            qq=qq,
            player_id=player_id,
            name=v["name"] if isinstance(v.get("name"), str) else "",
            email=v["email"] if isinstance(v.get("email"), str) else "",
            bound_at=int(v["boundAt"]) if isinstance(v.get("boundAt"), (int, float)) else 0,
        )

    def to_json(self) -> Dict[str, object]:
        """落盘用的形状（与 Bridge 契约一致，都是 camelCase）。"""
        return {
            "qq": self.qq,
            "playerId": self.player_id,
            "name": self.name,
            "email": self.email,
            "boundAt": self.bound_at,
        }


@dataclass
class PendingBind:
    """一条「已发码、等回码」的记录（内存态：进程重启后玩家重新 /绑定 即可）。"""

    qq: str
    email: str
    request_id: str
    expires_at: int


def default_data_dir(config_dir: str = "") -> Path:
    """数据目录：配置给了就用配置，否则 ``<cwd>/data/nonebot_plugin_stronghold_protocol``。

    特意**不**去 import ``nonebot_plugin_localstore``：那会把一个 NoneBot 插件模块塞进 ``sys.modules``，
    之后任何插件（例如 picmenu-next）再 ``require`` 它都会报「不是以插件方式加载」；
    而 localstore 自己也要求「以插件方式加载 + 能识别调用方插件」，在插件导入期本来就取不到。
    需要放到别处就配 ``stronghold_data_dir``。
    """
    if config_dir:
        return Path(config_dir)
    return Path.cwd() / "data" / "nonebot_plugin_stronghold_protocol"


class BindingStore:
    """绑定表的读写（JSON 文件，tmp + rename 原子写）。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_data_dir() / "bindings.json"
        self._lock = asyncio.Lock()
        self._by_qq: Dict[str, Binding] = {}
        self._by_player: Dict[str, Binding] = {}
        self._pending: Dict[str, PendingBind] = {}
        self._loaded = False

    # ---- 读写 ------------------------------------------------------------------------------------

    def load_sync(self) -> None:
        """同步读取文件（启动时调用一次即可；损坏/缺失都当空表）。"""
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            doc = json.loads(raw)
        except ValueError:
            return
        rows = doc.get("bindings") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            binding = Binding.from_json(row)
            if binding:
                self._by_qq[binding.qq] = binding
                self._by_player[binding.player_id] = binding

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load_sync()

    def _write_sync(self) -> None:
        doc = {
            "version": BINDING_FILE_VERSION,
            "savedAt": int(time.time() * 1000),
            "bindings": [b.to_json() for b in self._by_qq.values()],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # 存不下就放弃这次写入：绑定关系丢了顶多要玩家重绑，不能因此让机器人报错。
            pass

    # ---- 查询 ------------------------------------------------------------------------------------

    def get(self, qq: object) -> Optional[Binding]:
        self._ensure_loaded()
        return self._by_qq.get(str(qq))

    def get_by_player(self, player_id: str) -> Optional[Binding]:
        self._ensure_loaded()
        return self._by_player.get(player_id)

    def all(self) -> List[Binding]:
        self._ensure_loaded()
        return list(self._by_qq.values())

    # ---- 修改 ------------------------------------------------------------------------------------

    async def bind(self, qq: object, player_id: str, name: str = "", email: str = "") -> Binding:
        """写入/更新一条绑定（同一个 playerId 之前绑过别的 QQ 会被顶掉）。"""
        self._ensure_loaded()
        key = str(qq)
        binding = Binding(qq=key, player_id=player_id, name=name, email=email, bound_at=int(time.time() * 1000))
        async with self._lock:
            old = self._by_qq.get(key)
            if old and old.player_id != player_id:
                self._by_player.pop(old.player_id, None)
            previous = self._by_player.get(player_id)
            if previous and previous.qq != key:
                self._by_qq.pop(previous.qq, None)
            self._by_qq[key] = binding
            self._by_player[player_id] = binding
            self._write_sync()
        return binding

    async def unbind(self, qq: object) -> Optional[Binding]:
        """解绑；返回被删掉的绑定（没有则 None）。"""
        self._ensure_loaded()
        key = str(qq)
        async with self._lock:
            binding = self._by_qq.pop(key, None)
            if binding:
                self._by_player.pop(binding.player_id, None)
                self._write_sync()
        return binding

    # ---- 待验证请求 ------------------------------------------------------------------------------

    def put_pending(self, qq: object, email: str, request_id: str, ttl_ms: int) -> PendingBind:
        self._pending[str(qq)] = PendingBind(
            qq=str(qq),
            email=email,
            request_id=request_id,
            expires_at=int(time.time() * 1000) + max(0, ttl_ms),
        )
        return self._pending[str(qq)]

    def pop_pending(self, qq: object) -> Optional[PendingBind]:
        """取出并销毁待验证请求；过期的直接丢弃（``ttl`` 用尽即为过期）。"""
        key = str(qq)
        pending = self._pending.pop(key, None)
        if not pending:
            return None
        if pending.expires_at and pending.expires_at <= int(time.time() * 1000):
            return None
        return pending

    def peek_pending(self, qq: object) -> Optional[PendingBind]:
        return self._pending.get(str(qq))
