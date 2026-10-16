"""绑定表：跨进程持久化、换绑、解绑、待验证请求的过期。"""

from __future__ import annotations

import json

from nonebot_plugin_stronghold_protocol.storage import BindingStore


async def test_bind_and_lookup(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    binding = await store.bind("10001", "a_0000000001", "凯尔希", "10001@qq.com")
    assert binding.qq == "10001"
    assert store.get("10001").player_id == "a_0000000001"
    assert store.get("10001").name == "凯尔希"
    assert store.get_by_player("a_0000000001").qq == "10001"
    assert store.get(10001).player_id == "a_0000000001"  # int 与 str 同一个键
    assert len(store.all()) == 1


async def test_rebind_same_qq_moves_the_account(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    await store.bind("10001", "a_old", "旧号")
    await store.bind("10001", "a_new", "新号")
    assert store.get("10001").player_id == "a_new"
    assert store.get_by_player("a_old") is None
    assert len(store.all()) == 1


async def test_account_cannot_be_claimed_twice(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    await store.bind("10001", "a_1", "凯尔希")
    await store.bind("10002", "a_1", "凯尔希")
    assert store.get("10001") is None
    assert store.get("10002").player_id == "a_1"
    assert len(store.all()) == 1


async def test_unbind(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    await store.bind("10001", "a_1", "凯尔希")
    removed = await store.unbind("10001")
    assert removed.player_id == "a_1"
    assert store.get("10001") is None
    assert store.get_by_player("a_1") is None
    assert await store.unbind("10001") is None


async def test_persisted_and_reloaded(tmp_path):
    path = tmp_path / "bindings.json"
    store = BindingStore(path)
    await store.bind("10001", "a_1", "凯尔希", "1@qq.com")
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert on_disk["bindings"][0]["playerId"] == "a_1"

    fresh = BindingStore(path)
    assert fresh.get("10001").email == "1@qq.com"
    assert fresh.get_by_player("a_1").name == "凯尔希"


def test_corrupt_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = BindingStore(path)
    assert store.all() == []
    assert store.get("10001") is None


async def test_unwritable_path_does_not_raise(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    store = BindingStore(blocker / "bindings.json")  # 父目录是个文件 → mkdir 必然失败
    binding = await store.bind("10001", "a_1", "凯尔希")
    assert binding.player_id == "a_1"
    assert store.get("10001").player_id == "a_1"  # 内存里仍然生效


def test_pending_request_expires(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    pending = store.put_pending("10001", "a@qq.com", "b_1", ttl_ms=60_000)
    assert pending.request_id == "b_1"
    assert store.peek_pending("10001").email == "a@qq.com"
    assert store.pop_pending("10001").request_id == "b_1"
    assert store.pop_pending("10001") is None

    store.put_pending("10002", "b@qq.com", "b_2", ttl_ms=-1)  # 已过期
    assert store.pop_pending("10002") is None

    store.put_pending("10003", "c@qq.com", "b_3", ttl_ms=60_000)
    assert store.peek_pending("10003") is not None
    assert store.peek_pending("10003") is not None  # peek 不消费
