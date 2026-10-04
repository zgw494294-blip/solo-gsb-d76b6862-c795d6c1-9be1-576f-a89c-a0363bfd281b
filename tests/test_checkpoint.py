"""播放存档（checkpoint）：跨重启续播，保留未确认显示事件。

覆盖：
- 保存：仅已开通道且自动播放非运行（idle/paused/blocked）的会话可保存，
  否则 409；存档含完整快照、游标、隐藏集合、通道事件与
  next_seq/acked_seq/max_returned_seq；同名 409、未知或已结束会话 404、
  非法 id 422；成功不结束会话、失败不留存档；并发同名保存恰好一次成功；
- 恢复：按存档 id 恢复为新 session_id 的独立会话，不读取当前场景；事件、
  序号与确认边界保留，后续事件从 next_seq 编号；idle 仍 idle，paused/
  blocked 恢复为 paused，续播重新计时、不追赶离线时间；可重复恢复，
  后续操作不改存档或其他会话；
- 容量：恢复出的队列计入共享容量，不足 503 且不留会话或容量占用，
  并发恢复不超额，释放容量后可重试；
- 跨重启：存档落 SQLite，重置播放管理器（模拟进程重启）后仍可恢复续播。
"""
import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.playback import (  # noqa: E402
    AckOutOfRange,
    ChannelCapacityExhausted,
    PlaybackManager,
)
from app.storage import SceneRecord  # noqa: E402


def scene_payload(subtitles=None, track_count=2):
    """s1[0,1000) s2[800,3000)锁2 s3[1000,2500) —— 分轨 1/2/1，换轨 0。"""
    if subtitles is None:
        subtitles = [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ]
    return {
        "track_count": track_count,
        "expected_revision": 0,
        "subtitles": subtitles,
    }


def make_session(client, scene_id="scene-cp", **kwargs):
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload(**kwargs))
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()


def open_channel(client, sid):
    resp = client.post(f"/sessions/{sid}/channel")
    assert resp.status_code == 200
    return resp


def fetch(client, sid, ack_seq):
    return client.post(
        f"/sessions/{sid}/channel/fetch", json={"ack_seq": ack_seq}
    )


def save(client, sid, checkpoint_id):
    return client.post(
        f"/sessions/{sid}/checkpoints", json={"checkpoint_id": checkpoint_id}
    )


def restore(client, checkpoint_id):
    return client.post(f"/checkpoints/{checkpoint_id}/restore")


def autoplay(client, sid, action, expected=None):
    method = client.get if action == "status" else client.post
    suffix = "" if action == "status" else f"/{action}"
    resp = method(f"/sessions/{sid}/autoplay{suffix}")
    if expected is not None:
        assert resp.status_code == expected, resp.text
    return resp


def start(client, sid, expected=200):
    return autoplay(client, sid, "start", expected)


def pause(client, sid, expected=200):
    return autoplay(client, sid, "pause", expected)


def resume(client, sid, expected=200):
    return autoplay(client, sid, "resume", expected)


def status(client, sid):
    return autoplay(client, sid, "status").json()


def wait_for(condition, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise AssertionError("条件在超时时间内未满足")


def wait_cursor_at_least(client, sid, expected_ms, timeout=3.0):
    wait_for(
        lambda: status(client, sid)["cursor_ms"] >= expected_ms,
        timeout=timeout,
    )
    return status(client, sid)


def wait_status(client, sid, wanted, timeout=3.0):
    wait_for(
        lambda: status(client, sid)["status"] == wanted, timeout=timeout
    )
    return status(client, sid)


def _quads(body):
    return [
        (e["seq"], e["time_ms"], e["type"], e["id"]) for e in body["events"]
    ]


def _two_batch_subtitles():
    """两批事件：第一批 2 条，第二批 8 条（配合小容量触发 blocked）。"""
    return [
        {"id": "early", "speaker": "E", "start_ms": 100, "end_ms": 110},
        *[
            {
                "id": f"late{i}",
                "speaker": f"L{i}",
                "start_ms": 105 + i * 4,
                "end_ms": 105 + i * 4 + 2,
            }
            for i in range(4)
        ],
    ]


# ---------------------------------------------------------------------------
# 保存：前提、响应字段、成功不结束会话、失败不留存档
# ---------------------------------------------------------------------------


def test_save_returns_archive_coordinates_and_session_survives():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )
    fetch(client, sid, 0)  # 已返回到 seq 4
    fetch(client, sid, 2)  # 确认并删除前 2 条，队列剩 seq 3、4

    resp = save(client, sid, "cp-1")
    assert resp.status_code == 200
    assert resp.json() == {
        "checkpoint_id": "cp-1",
        "session_id": sid,
        "scene_id": "scene-cp",
        "source_revision": 1,
        "cursor_ms": 1000,
        "status": "idle",
        "queued_events": 2,
        "next_seq": 5,
        "acked_seq": 2,
        "max_returned_seq": 4,
    }
    # 成功不结束会话：推进、补取照常，队列与序号不因保存改变
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [3, 4]
    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 3000})
    assert resp.status_code == 200
    # 隐藏 Alice：s3 的 2500 退出被排除，只记录 s2 的 3000 退出（seq 5）
    assert _quads(fetch(client, sid, 2).json()) == [
        (3, 1000, "enter", "s3"),
        (4, 1000, "exit", "s3"),
        (5, 3000, "exit", "s2"),
    ]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200


def test_save_requires_open_channel_409():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    resp = save(client, sid, "cp-nochan")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "channel_missing"
    # 失败不留存档
    assert restore(client, "cp-nochan").status_code == 404


def test_save_while_running_409_then_pause_allows_save():
    client = TestClient(app)
    sid = make_session(client, "scene-cp-run")["session_id"]
    open_channel(client, sid)
    start(client, sid)
    resp = save(client, sid, "cp-run")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "autoplay_running"
    assert detail["status"] == "running"
    # 失败不留存档
    assert restore(client, "cp-run").status_code == 404
    # 暂停后即可保存，存档状态为 paused
    pause(client, sid)
    resp = save(client, sid, "cp-run")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    client.post(f"/sessions/{sid}/finish")


def test_save_unknown_or_finished_session_404():
    client = TestClient(app)
    assert save(client, "no-such-session", "cp-x").status_code == 404
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert save(client, sid, "cp-x").status_code == 404
    # 失败不留存档
    assert restore(client, "cp-x").status_code == 404


def test_duplicate_checkpoint_409_and_archive_untouched():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    assert save(client, sid, "cp-dup").status_code == 200
    # 同名存档：409，已有存档不被覆盖
    client.post(f"/sessions/{sid}/events", json={"time_ms": 3000})
    resp = save(client, sid, "cp-dup")
    assert resp.status_code == 409
    assert resp.json()["detail"]["checkpoint_id"] == "cp-dup"
    body = restore(client, "cp-dup").json()
    assert body["cursor_ms"] == 900
    # 存档保留的是保存时（游标 900）的队列：仅 s2 的 800 进入（seq 1）
    assert _quads(fetch(client, body["session_id"], 0).json()) == [
        (1, 800, "enter", "s2"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"checkpoint_id": ""},       # 空字符串
        {"checkpoint_id": "   "},    # 纯空白
        {"checkpoint_id": None},     # null
        {"checkpoint_id": 1},        # 数字不隐式转换
        {"checkpoint_id": True},     # 布尔不隐式转换
        {},                          # 字段缺失
    ],
)
def test_save_checkpoint_id_validation_422(payload):
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    resp = client.post(f"/sessions/{sid}/checkpoints", json=payload)
    assert resp.status_code == 422


def test_concurrent_save_same_name_exactly_one_succeeds():
    """并发保存同名存档在会话锁与数据库写锁内串行：恰好一次 200。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)

    results = []
    barrier = threading.Barrier(6)

    def worker():
        local = TestClient(app)
        barrier.wait()
        results.append(save(local, sid, "cp-race").status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409, 409, 409, 409, 409]
    assert restore(client, "cp-race").status_code == 200


# ---------------------------------------------------------------------------
# 恢复：新会话、快照、事件与确认边界、序号续编
# ---------------------------------------------------------------------------


def test_restore_unknown_checkpoint_404():
    client = TestClient(app)
    assert restore(client, "no-such-checkpoint").status_code == 404


def test_restore_blank_checkpoint_id_422():
    client = TestClient(app)
    assert client.post("/checkpoints/%20/restore").status_code == 422
    assert client.post("/checkpoints/%20%20%20/restore").status_code == 422


def test_save_restore_across_restart_preserves_events_and_boundaries():
    """跨重启续播主流程：存档落库 -> 模拟重启 -> 恢复 -> 补取 -> 续播。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )
    fetch(client, sid, 0)  # 已返回到 seq 4
    fetch(client, sid, 2)  # 确认并删除前 2 条，队列剩 seq 3、4
    assert save(client, sid, "cp-1").status_code == 200

    # 模拟进程重启：内存会话全部丢失（旧会话一律 404），存档仍在
    main_module.reset_playback()
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000})
    assert resp.status_code == 404

    resp = restore(client, "cp-1")
    assert resp.status_code == 200
    body = resp.json()
    new_sid = body["session_id"]
    assert new_sid != sid
    assert body["checkpoint_id"] == "cp-1"
    assert body["scene_id"] == "scene-cp"
    assert body["source_revision"] == 1
    assert body["cursor_ms"] == 1000
    assert body["track_count"] == 2
    assert body["hidden_speakers"] == ["Alice"]
    assert body["status"] == "idle"
    # 完整快照（含 locked_track 与分轨）原样恢复
    assert [
        (s["id"], s["speaker"], s["start_ms"], s["end_ms"],
         s["locked_track"])
        for s in body["subtitles"]
    ] == [
        ("s1", "Alice", 0, 1000, None),
        ("s2", "Bob", 800, 3000, 2),
        ("s3", "Alice", 1000, 2500, None),
    ]
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]

    # 未确认事件、序号与确认边界原样保留
    body = fetch(client, new_sid, 0).json()
    assert body["acked_seq"] == 2
    assert _quads(body) == [
        (3, 1000, "enter", "s3"),
        (4, 1000, "exit", "s3"),
    ]
    # 确认边界：超过已返回最大 seq（4）越界 422；旧确认不回退
    assert fetch(client, new_sid, 5).status_code == 422
    assert fetch(client, new_sid, 1).json()["acked_seq"] == 2
    # 续播：后续事件从 next_seq（5）继续编号；隐藏过滤照常
    client.post(f"/sessions/{new_sid}/events", json={"time_ms": 3000})
    assert _quads(fetch(client, new_sid, 2).json()) == [
        (3, 1000, "enter", "s3"),
        (4, 1000, "exit", "s3"),
        (5, 3000, "exit", "s2"),
    ]
    # 恢复出的会话是普通会话：结束照常
    assert client.post(f"/sessions/{new_sid}/finish").status_code == 200


def test_restore_does_not_read_current_scene():
    """恢复不读取当前场景：存档后的场景修订不纳入恢复出的会话。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    assert save(client, sid, "cp-scene").status_code == 200
    # 场景修订为 revision 2：s3 延长到 4000，新增 s4
    resp = client.put(
        "/scenes/scene-cp",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "s1", "speaker": "Alice", "start_ms": 0,
                 "end_ms": 1000},
                {"id": "s2", "speaker": "Bob", "start_ms": 800,
                 "end_ms": 3000, "locked_track": 2},
                {"id": "s3", "speaker": "Alice", "start_ms": 1000,
                 "end_ms": 4000},
                {"id": "s4", "speaker": "Carol", "start_ms": 3200,
                 "end_ms": 4000},
            ],
        },
    )
    assert resp.status_code == 200

    body = restore(client, "cp-scene").json()
    # 快照完全取自存档：仍是 revision 1 的三条字幕
    assert body["source_revision"] == 1
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3"]
    assert body["cursor_ms"] == 900
    # 恢复出的会话仍可正常接纳当前场景（accept 才读取场景）
    resp = client.post(f"/sessions/{body['session_id']}/accept")
    assert resp.status_code == 200
    assert resp.json()["source_revision"] == 2


def test_restore_is_repeatable_and_sessions_are_independent():
    """可重复恢复为独立会话：后续操作不改存档或其他会话。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert save(client, sid, "cp-repeat").status_code == 200

    a = restore(client, "cp-repeat").json()
    b = restore(client, "cp-repeat").json()
    assert a["session_id"] != b["session_id"]
    assert a["cursor_ms"] == b["cursor_ms"] == 1000

    # 推进 A 不影响 B；B 的队列仍是存档时的 seq 1~3
    client.post(f"/sessions/{a['session_id']}/events", json={"time_ms": 3000})
    assert _quads(fetch(client, a["session_id"], 0).json()) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
        (4, 2500, "exit", "s3"),
        (5, 3000, "exit", "s2"),
    ]
    assert [e["seq"] for e in fetch(client, b["session_id"], 0).json()[
        "events"]] == [1, 2, 3]
    # 结束 A 不影响 B；存档本身不被改动：第三次恢复仍是原样
    assert client.post(f"/sessions/{a['session_id']}/finish").status_code == 200
    assert [e["seq"] for e in fetch(client, b["session_id"], 0).json()[
        "events"]] == [1, 2, 3]
    c = restore(client, "cp-repeat").json()
    assert c["cursor_ms"] == 1000
    assert [e["seq"] for e in fetch(client, c["session_id"], 0).json()[
        "events"]] == [1, 2, 3]


def test_restore_works_after_original_session_finished():
    """存档独立于原会话：原会话结束后仍可恢复。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert save(client, sid, "cp-fin").status_code == 200
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    body = restore(client, "cp-fin").json()
    assert body["cursor_ms"] == 1000
    assert [e["seq"] for e in fetch(client, body["session_id"], 0).json()[
        "events"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 自动播放状态：idle 仍 idle；paused/blocked 恢复为 paused；不追赶离线时间
# ---------------------------------------------------------------------------


def test_idle_archive_restores_as_idle_and_can_start_autoplay():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    assert save(client, sid, "cp-idle").json()["status"] == "idle"
    body = restore(client, "cp-idle").json()
    assert body["status"] == "idle"
    new_sid = body["session_id"]
    assert status(client, new_sid)["status"] == "idle"
    # 未启动者仍未启动：可照常启动自动播放（通道已随存档恢复）
    assert start(client, new_sid).json()["status"] == "running"
    client.post(f"/sessions/{new_sid}/finish")


def test_paused_archive_restores_as_paused_without_catching_up():
    client = TestClient(app)
    sid = make_session(client, "scene-cp-pause")["session_id"]
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 150)
    retained = pause(client, sid).json()["cursor_ms"]
    resp = save(client, sid, "cp-pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    assert resp.json()["cursor_ms"] == retained

    main_module.reset_playback()  # 模拟重启：驱动状态丢失
    time.sleep(0.3)  # 离线（停机）时间不得计入

    body = restore(client, "cp-pause").json()
    new_sid = body["session_id"]
    assert body["status"] == "paused"
    assert body["cursor_ms"] == retained
    # 恢复为暂停：不计时
    time.sleep(0.15)
    assert status(client, new_sid)["cursor_ms"] == retained
    # 续播重新计时：只走恢复后的真实时间，不追赶离线的 0.45 秒
    resume(client, new_sid)
    wait_cursor_at_least(client, new_sid, retained + 40)
    assert status(client, new_sid)["cursor_ms"] < retained + 300
    client.post(f"/sessions/{new_sid}/finish")


def test_blocked_archive_restores_as_paused(monkeypatch):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "3")
    main_module.reset_playback()
    client = TestClient(app)
    sid = make_session(
        client, "scene-cp-block", subtitles=_two_batch_subtitles()
    )["session_id"]
    open_channel(client, sid)
    start(client, sid)
    blocked = wait_status(client, sid, "blocked")
    blocked_cursor = blocked["cursor_ms"]
    # 阻塞不是运行态：可以保存，存档状态为 blocked
    resp = save(client, sid, "cp-block")
    assert resp.status_code == 200
    assert resp.json()["status"] == "blocked"
    assert resp.json()["cursor_ms"] == blocked_cursor

    main_module.reset_playback()  # 模拟重启
    body = restore(client, "cp-block").json()
    # 阻塞的存档恢复为暂停；游标与未确认事件保留
    assert body["status"] == "paused"
    assert body["cursor_ms"] == blocked_cursor
    new_sid = body["session_id"]
    queued = fetch(client, new_sid, 0).json()["events"]
    assert [e["seq"] for e in queued] == list(range(1, len(queued) + 1))
    # 恢复为暂停后可显式续播（容量已随重启重置，下一拍整批入队）
    assert resume(client, new_sid).json()["status"] == "running"
    client.post(f"/sessions/{new_sid}/finish")


# ---------------------------------------------------------------------------
# 共享容量：恢复计入容量，不足 503 且不留会话或容量占用，并发不超额
# ---------------------------------------------------------------------------


def test_restore_insufficient_capacity_503_then_retry_after_release(
    monkeypatch,
):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "3")
    main_module.reset_playback()
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    # 3 条事件入队，占满共享容量
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert save(client, sid, "cp-cap").status_code == 200
    cap = main_module.get_playback().channel_capacity
    assert cap.used == 3

    # 恢复需要 3 个名额，剩余 0：503，不留会话或容量占用
    resp = restore(client, "cp-cap")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["limit"] == 3
    assert detail["requested"] == 3
    assert detail["available"] == 0
    assert cap.used == 3

    # 确认前缀释放容量后，同一存档重试成功
    assert fetch(client, sid, 0).status_code == 200
    fetch(client, sid, 3)
    assert cap.used == 0
    body = restore(client, "cp-cap").json()
    assert body["cursor_ms"] == 1000
    assert cap.used == 3  # 恢复出的队列计入共享容量
    # 恢复出的会话结束即按原规则释放其队列占用
    assert client.post(
        f"/sessions/{body['session_id']}/finish"
    ).status_code == 200
    assert cap.used == 0


def test_concurrent_restores_never_exceed_capacity(monkeypatch):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "5")
    main_module.reset_playback()
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})  # 3 条
    assert save(client, sid, "cp-conc").status_code == 200
    # 结束原会话释放其占用：容量归零，存档仍在
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    cap = main_module.get_playback().channel_capacity
    assert cap.used == 0

    results = []
    barrier = threading.Barrier(2)

    def worker():
        local = TestClient(app)
        barrier.wait()
        results.append(local.post("/checkpoints/cp-conc/restore").status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 每个恢复需要 3 个名额，上限 5：恰好一个成功，并发不超额
    assert sorted(results) == [200, 503]
    assert cap.used == 3


# ---------------------------------------------------------------------------
# 管理器级确定性测试：失败不留会话/容量/存档，存档内容不被后续操作修改
# ---------------------------------------------------------------------------


def _record(scene_id="scene-cp", revision=1):
    subtitles = [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000,
         "locked_track": None},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500,
         "locked_track": None},
    ]
    return SceneRecord(
        scene_id=scene_id,
        revision=revision,
        track_count=2,
        subtitles=[dict(s) for s in subtitles],
        assignments=[
            {"id": "s1", "track": 1},
            {"id": "s2", "track": 2},
            {"id": "s3", "track": 1},
        ],
        changes=0,
    )


def _archive_payload(event_count):
    events = [
        {
            "seq": i + 1,
            "id": f"e{i + 1}",
            "speaker": "S",
            "track": 1,
            "time_ms": 10 * (i + 1),
            "type": "enter",
        }
        for i in range(event_count)
    ]
    return {
        "scene_id": "scene-x",
        "source_revision": 1,
        "track_count": 2,
        "changes": 0,
        "cursor_ms": 100,
        "subtitles": [
            {"id": "a", "speaker": "S", "start_ms": 0, "end_ms": 1000,
             "locked_track": None},
        ],
        "assignments": [{"id": "a", "track": 1}],
        "hidden_speakers": [],
        "autoplay_status": "paused",
        "channel": {
            "events": events,
            "next_seq": event_count + 1,
            "acked_seq": 0,
            "max_returned_seq": event_count,
        },
    }


def test_manager_restore_503_leaves_no_session_or_capacity():
    pm = PlaybackManager(channel_event_limit=2, autoplay_tick_interval_s=None)
    with pytest.raises(ChannelCapacityExhausted):
        pm.restore_checkpoint("cp", _archive_payload(event_count=3))
    assert pm.channel_capacity.used == 0
    assert pm._sessions == {}


def test_manager_save_failure_leaves_no_archive_and_session_untouched():
    pm = PlaybackManager(channel_event_limit=16, autoplay_tick_interval_s=None)
    session = pm.get(pm.create(_record())["session_id"])
    pm.open_channel(session)
    pm.advance_events(session, 1000)
    saved = {}

    def failing_commit(checkpoint_id, payload):
        raise RuntimeError("落库失败")

    with pytest.raises(RuntimeError):
        pm.save_checkpoint(session, "cp-1", failing_commit)
    assert saved == {}
    # 失败不影响会话与通道：补取、推进照常
    assert [e["seq"] for e in pm.fetch_channel(session, 0)["events"]] == [
        1, 2, 3,
    ]
    assert pm.advance_events(session, 3000)["cursor_ms"] == 3000


def test_manager_save_restore_roundtrip_and_archive_immutable():
    pm = PlaybackManager(channel_event_limit=16, autoplay_tick_interval_s=None)
    record = _record(revision=3)
    session = pm.get(pm.create(record)["session_id"])
    pm.open_channel(session)
    pm.advance_events(session, 1000)          # seq 1~3
    pm.set_hidden_speakers(session, ["Alice"])  # seq 4（s3 exit@1000）
    pm.fetch_channel(session, 0)              # 已返回到 4
    pm.fetch_channel(session, 2)              # 确认前 2 条，队列剩 3、4

    archives = {}
    summary = pm.save_checkpoint(
        session, "cp-1", lambda cid, p: archives.update({cid: p})
    )
    assert summary["queued_events"] == 2
    assert summary["next_seq"] == 5
    assert summary["acked_seq"] == 2
    assert summary["max_returned_seq"] == 4
    assert summary["status"] == "idle"
    # 成功不结束会话
    assert pm.fetch_channel(session, 0)["events"][0]["seq"] == 3

    # 新管理器（模拟重启）从存档恢复
    pm2 = PlaybackManager(channel_event_limit=16, autoplay_tick_interval_s=None)
    restored = pm2.restore_checkpoint("cp-1", archives["cp-1"])
    assert restored["session_id"] != session.session_id
    assert restored["cursor_ms"] == 1000
    assert restored["source_revision"] == 3
    assert restored["hidden_speakers"] == ["Alice"]
    assert restored["status"] == "idle"
    assert pm2.channel_capacity.used == 2

    s2 = pm2.get(restored["session_id"])
    fetched = pm2.fetch_channel(s2, 0)
    assert fetched["acked_seq"] == 2
    assert [e["seq"] for e in fetched["events"]] == [3, 4]
    with pytest.raises(AckOutOfRange):
        pm2.fetch_channel(s2, 5)  # 已返回最大 seq 为 4
    # 后续事件从 next_seq（5）继续编号（Alice 隐藏：只有 s2 的退出）
    pm2.advance_events(s2, 3000)
    assert [e["seq"] for e in pm2.fetch_channel(s2, 2)["events"]] == [3, 4, 5]
    # 存档本身不被恢复后的操作修改
    assert archives["cp-1"]["channel"]["next_seq"] == 5
    assert [e["seq"] for e in archives["cp-1"]["channel"]["events"]] == [3, 4]
    assert archives["cp-1"]["cursor_ms"] == 1000
