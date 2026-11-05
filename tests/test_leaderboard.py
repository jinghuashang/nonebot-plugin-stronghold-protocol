"""排行榜兜底：WS 地址推导、solo/multi 合并、缓存与失败降级。"""

from __future__ import annotations

from nonebot_plugin_stronghold_protocol.leaderboard import LeaderboardClient, merge_unite, ws_url_from


def board(scope: str, rows: list, bid: str = "unite"):
    return {"id": bid, "scope": scope, "rows": rows, "name": "兜底之王"}


def row(pid: str, name: str, kills: int, best: int = 0, matches: int = 1, last_at: int = 1):
    return {"playerId": pid, "name": name, "kills": kills, "best": best, "matches": matches, "lastAt": last_at}


def test_ws_url_from():
    assert ws_url_from("https://sp.example.com") == "wss://sp.example.com/ws"
    assert ws_url_from("http://127.0.0.1:3000/") == "ws://127.0.0.1:3000/ws"
    assert ws_url_from("wss://sp.example.com/ws") == "wss://sp.example.com/ws"
    assert ws_url_from("") == ""


def test_merge_unite_sums_solo_and_multi_and_skips_fun():
    boards = [
        board("solo", [row("a_1", "阿米娅", 10, best=4), row("a_2", "凯尔希", 5, best=5)]),
        board("multi", [row("a_1", "阿米娅", 7, best=9), row("a_3", "陈", 3)]),
        board("fun", [row("a_9", "AI·碎骨", 999)]),
        board("solo", [row("a_1", "阿米娅", 1)], bid="worst"),
    ]
    merged = merge_unite(boards)
    assert merged is not None and merged.kind == "unite"
    assert "累计" in merged.note
    assert [h.name for h in merged.holders] == ["阿米娅", "凯尔希", "陈"], "按累计击退排序，娱乐房不混进来"
    top = merged.holders[0]
    assert top.kills == 17 and top.best == 9 and top.matches == 2, "两个分场相加、单局最多取最大"
    assert merged.total == 3


def test_merge_unite_without_rows_is_none():
    assert merge_unite([]) is None
    assert merge_unite([board("solo", [row("a_1", "阿米娅", 0)])]) is None


async def test_client_caches_and_degrades():
    client = LeaderboardClient("ws://test/ws", cache_seconds=60)
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return [board("solo", [row("a_1", "阿米娅", 3)])]

    client._fetch = fake_fetch  # type: ignore[assignment]
    first = await client.unite()
    second = await client.unite()
    assert first is not None and second is first
    assert calls["n"] == 1, "缓存期内不重复占游戏会话"

    async def boom():
        calls["n"] += 1
        raise RuntimeError("ws down")

    client2 = LeaderboardClient("ws://test/ws")
    client2._fetch = boom  # type: ignore[assignment]
    assert await client2.unite() is None, "读不到就是 None，不能抛给日报"


async def test_client_without_url_does_nothing():
    assert await LeaderboardClient("").unite() is None
