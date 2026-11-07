"""公告同步：首次不补发、新公告只发一次、过期跳过、补发开关。"""

from __future__ import annotations

import json

import pytest

from nonebot_plugin_stronghold_protocol import announce as announce_mod
from nonebot_plugin_stronghold_protocol.announce import AnnouncementPoller
from nonebot_plugin_stronghold_protocol.config import Config
from nonebot_plugin_stronghold_protocol.service import StrongholdService
from nonebot_plugin_stronghold_protocol.storage import BindingStore

GROUP = 1128024089


def make_service(tmp_path, **cfg):
    cfg.setdefault("stronghold_manage_groups", [GROUP])
    cfg.setdefault("stronghold_data_dir", str(tmp_path))
    return StrongholdService(Config(**cfg), store=BindingStore(tmp_path / "bindings.json"))


def item(announce_id, title="更新预告", body="今晚 23:00 重启", level="warn", until=None):
    return {"id": announce_id, "title": title, "body": body, "level": level, "until": until}


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(announce_mod, "get_bots", lambda: {"1": object()})

    async def fake_broadcast(text, groups, image=None):
        calls.append({"text": text, "groups": list(groups), "image": image})
        return [f"{g} 已发送（图片）" for g in groups]

    async def fake_image(kind, context):
        return b"img"

    monkeypatch.setattr(announce_mod, "broadcast", fake_broadcast)
    monkeypatch.setattr(announce_mod, "build_image", fake_image)
    return calls


async def test_first_run_marks_seen_without_sending(tmp_path, sent):
    service = make_service(tmp_path)
    service.client.announcements = lambda: _async([item("old")])
    poller = AnnouncementPoller(service)

    assert await poller.poll() == []
    assert sent == []
    state = json.loads((tmp_path / "announcements-seen.json").read_text(encoding="utf-8"))
    assert "old" in state["seen"]


async def test_new_announcement_is_sent_once(tmp_path, sent):
    service = make_service(tmp_path)
    poller = AnnouncementPoller(service)
    payload = {"items": []}

    service.client.announcements = lambda: _async(list(payload["items"]))
    await poller.poll()  # 首次：空列表，建立状态文件

    payload["items"] = [item("new-1")]
    results = await poller.poll()
    assert results == [f"{GROUP} 已发送（图片）"]
    assert len(sent) == 1
    assert sent[0]["groups"] == [GROUP] and sent[0]["image"] == b"img"
    assert "服务器公告" in sent[0]["text"] and "今晚 23:00 重启" in sent[0]["text"]

    assert await poller.poll() == [], "同一条公告不能重复发"
    assert len(sent) == 1


async def test_expired_announcement_is_skipped(tmp_path, sent):
    service = make_service(tmp_path)
    poller = AnnouncementPoller(service)
    await poller.poll()

    service.client.announcements = lambda: _async([item("expired", until=1)])
    assert await poller.poll() == []
    assert sent == []


async def test_backfill_sends_existing_on_first_run(tmp_path, sent):
    service = make_service(tmp_path, stronghold_announce_backfill=True)
    service.client.announcements = lambda: _async([item("already")])
    poller = AnnouncementPoller(service)

    results = await poller.poll()
    assert results == [f"{GROUP} 已发送（图片）"]
    assert len(sent) == 1


async def test_no_groups_means_no_fetch(tmp_path, sent):
    service = make_service(tmp_path, stronghold_manage_groups=[])
    called = {"n": 0}

    async def fetch():
        called["n"] += 1
        return [item("x")]

    service.client.announcements = fetch
    poller = AnnouncementPoller(service)
    assert await poller.poll() == []
    assert called["n"] == 0, "没白名单群就别去打服务器"


async def _async(value):
    return value


async def test_no_bot_connected_does_not_mark_seen(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.client.announcements = lambda: _async([item("later")])
    monkeypatch.setattr(announce_mod, "get_bots", lambda: {})
    poller = AnnouncementPoller(service)

    assert await poller.poll() == []
    assert not (tmp_path / "announcements-seen.json").exists(), "机器人没连上时不能把公告记成已读"


async def test_backfill_sends_only_the_newest(tmp_path, sent):
    old = item("old", title="旧公告", until=None)
    old["at"] = 100
    new = item("new", title="新公告", until=None)
    new["at"] = 200
    service = make_service(tmp_path, stronghold_announce_backfill=True)
    service.client.announcements = lambda: _async([old, new])
    poller = AnnouncementPoller(service)

    await poller.poll()
    assert len(sent) == 1 and "新公告" in sent[0]["text"], "补发只发最新一条"
    state = json.loads((tmp_path / "announcements-seen.json").read_text(encoding="utf-8"))
    assert "old" in state["seen"] and "new" in state["seen"]
