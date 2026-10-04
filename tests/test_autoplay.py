"""自动播放（autoplay）：会话按真实经过时间自动推进。

覆盖：
- 状态机：启动仅限已开补取通道且未启动（idle）；暂停仅限 running；
  恢复仅限 paused/blocked；查询返回 idle/running/paused/blocked 与游标；
  不符一律 409，未知/已结束会话 404；
- 计时：后台每 50ms 推进，目标为本段起始游标 + 单调时钟经过的整毫秒；
  暂停、阻塞不计时，恢复从保留游标重新计时，不追赶停顿时间；
- 沿用事件窗口、隐藏过滤、排序、seq 与共享容量：一次推进整批入队，
  容量不足转为 blocked（游标、seq、队列不变），确认释放后须显式恢复；
- 运行中两种手动推进 409，其他既有会话操作（隐藏切换、接纳、补取、
  结束）在各状态下仍可用并完整串行；暂停返回后旧任务不再推进；
- 结束停止驱动、按原规则释放容量、后续请求 404；驱动仅存单进程内存。
"""
import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402


def scene_payload(scene_id="scene-auto", subtitles=None, track_count=2):
    if subtitles is None:
        # s1[0,1000) s2[800,3000)锁2 s3[1000,2500)：分轨 1/2/1
        subtitles = [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ]
    resp = TestClient(app).put(
        f"/scenes/{scene_id}",
        json={
            "track_count": track_count,
            "expected_revision": 0,
            "subtitles": subtitles,
        },
    )
    assert resp.status_code == 200


def make_session(client, scene_id="scene-auto", **kwargs):
    scene_payload(scene_id=scene_id, **kwargs)
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def open_channel(client, sid):
    resp = client.post(f"/sessions/{sid}/channel")
    assert resp.status_code == 200
    return resp.json()


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


def fetch(client, sid, ack_seq):
    return client.post(
        f"/sessions/{sid}/channel/fetch", json={"ack_seq": ack_seq}
    )


def wait_for(condition, timeout=3.0, interval=0.01):
    """轮询直到 condition() 为真；超时断言失败。"""
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


# ---------------------------------------------------------------------------
# 查询与状态机
# ---------------------------------------------------------------------------


def test_query_before_start_is_idle_with_cursor():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-idle")
    body = status(client, sid)
    assert body == {"session_id": sid, "status": "idle", "cursor_ms": 0}
    # 手动推进后查询：仍未启动，游标反映手动推进
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 120})
    body = status(client, sid)
    assert body["status"] == "idle"
    assert body["cursor_ms"] == 120


def test_start_requires_channel_409():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-nochan")
    resp = start(client, sid, expected=409)
    assert resp.json()["detail"]["reason"] == "channel_missing"
    # 会话仍为 idle
    assert status(client, sid)["status"] == "idle"
    # 开启通道后可以启动
    open_channel(client, sid)
    assert start(client, sid).status_code == 200


def test_start_twice_is_409_and_pause_resume_rules():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-rules")
    open_channel(client, sid)
    assert start(client, sid).json()["status"] == "running"
    # 运行中再启动 409
    resp = start(client, sid, expected=409)
    assert resp.json()["detail"]["reason"] == "already_started"
    # 运行中恢复 409
    resp = resume(client, sid, expected=409)
    assert resp.json()["detail"]["reason"] == "resume_paused_or_blocked"
    # 暂停成功
    assert pause(client, sid).json()["status"] == "paused"
    # 暂停中启动 409、暂停 409
    assert start(client, sid, expected=409).json()["detail"]["reason"] == (
        "already_started"
    )
    assert pause(client, sid, expected=409).json()["detail"]["reason"] == (
        "pause_only_running"
    )
    # 恢复成功
    assert resume(client, sid).json()["status"] == "running"


def test_controls_on_never_started_session_409():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-never")
    open_channel(client, sid)
    assert pause(client, sid, expected=409).json()["detail"]["reason"] == (
        "not_started"
    )
    assert resume(client, sid, expected=409).json()["detail"]["reason"] == (
        "not_started"
    )


def test_unknown_session_is_404():
    client = TestClient(app)
    for resp in (
        client.post("/sessions/nope/autoplay/start"),
        client.post("/sessions/nope/autoplay/pause"),
        client.post("/sessions/nope/autoplay/resume"),
        client.get("/sessions/nope/autoplay"),
    ):
        assert resp.status_code == 404


def test_409_detail_carries_reason_and_status():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-detail")
    open_channel(client, sid)
    start(client, sid)
    pause(client, sid)
    detail = pause(client, sid, expected=409).json()["detail"]
    assert detail["reason"] == "pause_only_running"
    assert detail["status"] == "paused"
    assert "message" in detail


# ---------------------------------------------------------------------------
# 真实时间推进：游标按单调时钟增长
# ---------------------------------------------------------------------------


def test_cursor_advances_with_elapsed_wall_time():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-clock")
    open_channel(client, sid)
    t0 = time.monotonic()
    body = start(client, sid).json()
    assert body == {"session_id": sid, "status": "running", "cursor_ms": 0}

    body = wait_cursor_at_least(client, sid, 200)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert body["status"] == "running"
    # 游标不应超过真实经过时间（整毫秒，可能略少：最多一拍粒度）
    assert body["cursor_ms"] <= elapsed_ms + 5
    # 且明显已在推进（不是停在 0）
    assert body["cursor_ms"] >= 190


def test_start_uses_current_cursor_as_segment_base():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-base")
    open_channel(client, sid)
    # 启动前先手动推进到 500：本段起点为 500，而非 0
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 500}
    ).status_code == 200
    start(client, sid)
    body = wait_cursor_at_least(client, sid, 650)
    # 游标是在 500 基础上叠加经过时间，而不是从 0 重新开始
    assert body["cursor_ms"] >= 650


def test_tick_enqueues_full_event_batches_with_seq():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-events")
    opened = open_channel(client, sid)
    assert opened["seq"] == 0
    start(client, sid)
    # 推进经过 1000（s1 退出）、800 的进入在 0 时已由开启响应 active 给出，
    # 事件流首条应是 s2 在 800 的 enter——等待游标越过 1000。
    wait_cursor_at_least(client, sid, 1001)
    body = fetch(client, sid, 0).json()
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == list(range(1, len(seqs) + 1))
    by_key = {(e["time_ms"], e["type"], e["id"]): e["seq"] for e in body["events"]}
    # s2@800 enter 必须早于 s1@1000 exit / s3@1000 enter
    assert by_key[(800, "enter", "s2")] < by_key[(1000, "exit", "s1")]
    assert by_key[(1000, "exit", "s1")] < by_key[(1000, "enter", "s3")]
    # 整批入队，seq 连续无空洞
    client.post(f"/sessions/{sid}/finish")


# ---------------------------------------------------------------------------
# 暂停：不计时、保留游标、旧任务不再推进
# ---------------------------------------------------------------------------


def test_pause_freezes_cursor_and_resume_restarts_from_retained():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-pause")
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 200)
    paused = pause(client, sid).json()
    assert paused["status"] == "paused"
    retained = paused["cursor_ms"]
    assert retained >= 200
    # 暂停不计时：再等也不动
    time.sleep(0.25)
    assert status(client, sid)["cursor_ms"] == retained

    # 恢复后从保留游标继续，不追赶停顿的 250ms
    resume(client, sid)
    time.sleep(0.12)
    body = status(client, sid)
    assert body["status"] == "running"
    # 恢复后只走了约 120ms，绝不能把暂停期间的时间补回来（上界给节拍余量）
    assert body["cursor_ms"] < retained + 220, (retained, body["cursor_ms"])
    assert body["cursor_ms"] >= retained
    client.post(f"/sessions/{sid}/finish")


def test_pause_returns_before_old_task_can_advance():
    """暂停响应后，任何在途后台 tick 都不得再推进游标。"""
    client = TestClient(app)
    sid = make_session(client, "scene-auto-oldtask")
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 100)
    frozen = pause(client, sid).json()["cursor_ms"]
    # 连续多次查询 + 等待：游标稳定在暂停值
    for _ in range(5):
        time.sleep(0.03)
        assert status(client, sid)["cursor_ms"] == frozen
    client.post(f"/sessions/{sid}/finish")


def test_manual_advance_allowed_while_paused():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-manualpause")
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 50)
    pause(client, sid)
    retained = status(client, sid)["cursor_ms"]
    # 暂停态允许手动推进（只能不减）
    resp = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": retained + 100}
    )
    assert resp.status_code == 200
    assert resp.json()["cursor_ms"] == retained + 100
    # 恢复后以手动推进后的游标为新一段起点
    resume(client, sid)
    body = wait_cursor_at_least(client, sid, retained + 150)
    assert body["cursor_ms"] >= retained + 150
    client.post(f"/sessions/{sid}/finish")


# ---------------------------------------------------------------------------
# 运行中手动推进 409；既有操作在运行态仍可用
# ---------------------------------------------------------------------------


def test_manual_advances_are_409_while_running():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-409")
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 10)
    for path in ("/advance", "/events"):
        resp = client.post(
            f"/sessions/{sid}{path}", json={"time_ms": 900000}
        )
        assert resp.status_code == 409, (path, resp.text)
        assert resp.json()["detail"]["reason"] == "manual_advance_while_running"
    # 倒退的 422 被运行态 409 优先覆盖也无妨；暂停后手动推进恢复可用
    pause(client, sid)
    assert client.post(
        f"/sessions/{sid}/events", json={"time_ms": 50}
    ).status_code in (200, 422)  # 游标可能已越过 50
    client.post(f"/sessions/{sid}/finish")


def test_hidden_switch_and_channel_fetch_work_while_running():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-ops")
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 900)
    # 运行中切换隐藏：完整串行，返回 200（此时 s2 仍活动，隐藏 Bob 产生 exit）
    resp = client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Bob"]},
    )
    assert resp.status_code == 200
    assert all(a["speaker"] != "Bob" for a in resp.json()["active"])
    # 运行中补取：200，事件 seq 连续
    assert fetch(client, sid, 0).status_code == 200
    client.post(f"/sessions/{sid}/finish")


def test_accept_works_while_running_and_serializes():
    client = TestClient(app)
    scene_id = "scene-auto-accept"
    sid = make_session(client, scene_id)
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 50)
    # 场景修订（延长未开始的 s3、新增 s4），会话接纳成功
    client.put(
        f"/scenes/{scene_id}",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
                {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
                 "locked_track": 2},
                {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
                {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
            ],
        },
    )
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    assert resp.json()["source_revision"] == 2
    assert status(client, sid)["status"] == "running"
    client.post(f"/sessions/{sid}/finish")


# ---------------------------------------------------------------------------
# 满额阻塞：整批失败转 blocked，游标/seq/队列不变，确认后须显式恢复
# ---------------------------------------------------------------------------


def _two_batch_subtitles():
    """两批事件：第一批 2 条（s1 在 [100,110) 短字幕成对 enter/exit，
    时间在 100～110ms），第二批 8 条（105～122ms 内 4 条极短字幕的
    成对事件）。节拍 50ms 下首批（目标约 100ms）先入队 2 条；下一拍目标
    约 150ms 时整批 8 条无法入队 -> blocked。
    """
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


def test_blocked_smoke_over_http(monkeypatch):
    """HTTP 层面冒烟：满额后后台自动进入 blocked，且确认后不自动恢复。

    各拍切到的窗口边界依赖墙钟节拍，这里只断言确定成立的性质：
    游标停在阻塞值、队列是已排序事件的一个前缀、确认后仍为 blocked，
    精确的整批定序在下面的确定性管理器测试中覆盖。
    """
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "3")
    main_module.reset_playback()
    client = TestClient(app)
    sid = make_session(
        client, "scene-auto-block", subtitles=_two_batch_subtitles()
    )
    open_channel(client, sid)
    start(client, sid)

    body = wait_status(client, sid, "blocked")
    blocked_cursor = body["cursor_ms"]
    # 阻塞发生在事件开始（100ms）之后的某个拍上
    assert blocked_cursor >= 100
    time.sleep(0.2)
    assert status(client, sid)["cursor_ms"] == blocked_cursor
    # 已入队事件是排序后事件流的一个长度不超过 3 的前缀，seq 连续；
    # 首个失败的拍可能只产生 1～3 条事件，故队列允许为空之外的任意前缀。
    queued = fetch(client, sid, 0).json()["events"]
    assert 1 <= len(queued) <= 3
    assert [e["seq"] for e in queued] == list(range(1, len(queued) + 1))
    cap = main_module.get_playback().channel_capacity
    assert cap.used == len(queued)
    # 阻塞中暂停/再启动均 409
    assert pause(client, sid, expected=409).json()["detail"]["reason"] == (
        "pause_only_running"
    )
    assert start(client, sid, expected=409).json()["detail"]["reason"] == (
        "already_started"
    )
    # 确认释放后不会自动恢复
    fetch(client, sid, len(queued))
    time.sleep(0.15)
    assert status(client, sid)["status"] == "blocked"
    assert status(client, sid)["cursor_ms"] == blocked_cursor
    client.post(f"/sessions/{sid}/finish")


# ---------------------------------------------------------------------------
# 确定性管理器测试：注入单调时钟、显式 tick，精确定序
# ---------------------------------------------------------------------------


from app.playback import (  # noqa: E402
    AUTOPLAY_BLOCKED,
    AUTOPLAY_PAUSED,
    AUTOPLAY_RUNNING,
    AutoplayConflict,
    PlaybackManager,
)
from app.storage import SceneRecord  # noqa: E402


def _deterministic_manager(limit=2):
    """无后台线程的管理器 + 一个已开通道的会话（游标 0）。"""
    pm = PlaybackManager(
        channel_event_limit=limit, autoplay_tick_interval_s=None
    )
    subs = [dict(s) for s in _two_batch_subtitles()]
    track_count = 2
    assignments = [
        {"id": s["id"], "track": i % track_count + 1}
        for i, s in enumerate(subs)
    ]
    record = SceneRecord(
        scene_id="scene-det",
        revision=1,
        track_count=track_count,
        subtitles=subs,
        assignments=assignments,
        changes=0,
    )
    session_state = pm.create(record)
    session = pm.get(session_state["session_id"])
    pm.open_channel(session)
    return pm, session


def _queued(pm, session, ack=0):
    return pm.fetch_channel(session, ack)["events"]


def _record(scene_id, subtitles, track_count=2):
    subs = [dict(s) for s in subtitles]
    assignments = [
        {"id": s["id"], "track": i % track_count + 1}
        for i, s in enumerate(subs)
    ]
    return SceneRecord(
        scene_id=scene_id,
        revision=1,
        track_count=track_count,
        subtitles=subs,
        assignments=assignments,
        changes=0,
    )


def _open_session(pm, record):
    state = pm.create(record)
    session = pm.get(state["session_id"])
    pm.open_channel(session)
    return session


def test_deterministic_block_keeps_cursor_seq_queue_then_explicit_resume():
    # 上限 10：另开一个“占额”会话先占 9 条，使本会话首批之后容量不足而
    # 阻塞；结束占额会话释放后显式恢复，剩余整批即可继续入队。
    pm = PlaybackManager(
        channel_event_limit=10, autoplay_tick_interval_s=None
    )
    s = _open_session(pm, _record("scene-det", _two_batch_subtitles()))
    # 9 条字幕的 enter 时刻分别为 10,20,...,90（均非 0，不属初始活动）；
    # end 放在窗口之后，使推进到 90 只产生 9 条 enter
    hog_subs = [
        {
            "id": f"h{i}",
            "speaker": f"H{i}",
            "start_ms": 10 + i * 10,
            "end_ms": 10 + i * 10 + 500,
        }
        for i in range(9)
    ]
    hog = _open_session(pm, _record("scene-hog", hog_subs))
    pm.advance_events(hog, 90)  # 9 条 enter 入队，used=9
    assert pm.channel_capacity.used == 9

    # 本会话启动；第一拍 100ms：early enter 1 条恰好入队，used=10
    pm.start_autoplay(s, now=0.0)
    pm.tick_autoplay(now=0.100)
    assert s.cursor_ms == 100
    assert [(e["seq"], e["id"], e["type"]) for e in _queued(pm, s)] == [
        (1, "early", "enter"),
    ]

    # 第二拍 110ms：窗口 (100,110] 含 5 条事件，剩余名额 0 -> blocked
    pm.tick_autoplay(now=0.110)
    assert s.autoplay_status == AUTOPLAY_BLOCKED
    assert s.cursor_ms == 100
    assert pm.channel_capacity.used == 10
    assert [e["seq"] for e in _queued(pm, s)] == [1]

    # 再 tick（哪怕墙钟继续走）也不推进
    pm.tick_autoplay(now=5.0)
    assert s.cursor_ms == 100
    assert s.autoplay_status == AUTOPLAY_BLOCKED

    # 确认 seq 1 后占额会话仍占 9：下一批 5 条放不下，仍 blocked
    pm.fetch_channel(s, 1)
    assert pm.channel_capacity.used == 9
    pm.tick_autoplay(now=6.0)
    assert s.autoplay_status == AUTOPLAY_BLOCKED
    assert s.cursor_ms == 100

    # 结束占额会话释放 9 个名额——不会自动恢复（仍 blocked）
    pm.finish(hog.session_id)
    assert pm.channel_capacity.used == 0
    assert s.autoplay_status == AUTOPLAY_BLOCKED

    # 显式恢复：以保留游标 100 为新一段起点（now=10.0）
    pm.resume_autoplay(s, now=10.0)
    # 恢复后 5ms 的拍目标 105：窗口 (100,105] 仅 late0 enter，
    # 证明从保留游标继续而非追赶停顿
    pm.tick_autoplay(now=10.005)
    assert s.cursor_ms == 105
    assert [e["seq"] for e in _queued(pm, s)] == [2]
    # 下一拍整批放下剩余全部 8 条事件：容量充足 -> running，seq 连续到 10
    pm.tick_autoplay(now=11.0)
    assert s.autoplay_status == AUTOPLAY_RUNNING
    assert s.cursor_ms == 1100
    assert [e["seq"] for e in _queued(pm, s)] == list(range(2, 11))


def test_deterministic_pause_does_not_count_time_and_resume_rebases():
    pm, s = _deterministic_manager(limit=64)
    pm.start_autoplay(s, now=0.0)
    pm.tick_autoplay(now=0.100)  # 游标到 100
    assert s.cursor_ms == 100

    # 暂停：保留游标 100；暂停期间（时钟走到 9.0）不计时
    assert pm.pause_autoplay(s)["status"] == AUTOPLAY_PAUSED
    pm.tick_autoplay(now=9.0)
    assert s.cursor_ms == 100

    # 恢复以 100 为新一段起点：恢复后 125ms 的拍到 225，不追赶停顿
    # （0.125 是二进制可精确表示的秒数，避免浮点截断造成整毫秒差 1）
    pm.resume_autoplay(s, now=10.0)
    pm.tick_autoplay(now=10.125)
    assert s.cursor_ms == 225
    assert s.autoplay_status == AUTOPLAY_RUNNING


def test_deterministic_resume_without_capacity_reblocks():
    pm, s = _deterministic_manager(limit=2)
    pm.start_autoplay(s, now=0.0)
    pm.tick_autoplay(now=0.100)  # seq1 入队（used=1）
    pm.tick_autoplay(now=0.110)  # blocked
    assert s.autoplay_status == AUTOPLAY_BLOCKED
    cursor_at_block = s.cursor_ms
    # 不确认释放即恢复：下一批仍放不下 -> 再次 blocked，游标不变
    pm.resume_autoplay(s, now=1.0)
    pm.tick_autoplay(now=2.0)
    assert s.autoplay_status == AUTOPLAY_BLOCKED
    assert s.cursor_ms == cursor_at_block
    assert [e["seq"] for e in _queued(pm, s)] == [1]


def test_deterministic_manual_advance_409_only_while_running():
    pm, s = _deterministic_manager(limit=64)
    pm.start_autoplay(s, now=0.0)
    pm.tick_autoplay(now=0.050)
    with pytest.raises(AutoplayConflict):
        pm.advance(s, 100000)
    with pytest.raises(AutoplayConflict):
        pm.advance_events(s, 100000)
    pm.pause_autoplay(s)
    # 暂停态手动推进正常（事件整批入队、seq 继续编号）
    state = pm.advance_events(s, 130)
    assert state["cursor_ms"] == 130
    queued = _queued(pm, s)
    assert [e["seq"] for e in queued] == list(range(1, len(queued) + 1))

    # 阻塞态同样允许手动推进：满额时按既有规则整批失败（HTTP 503）
    from app.playback import ChannelCapacityExhausted

    pm2, b = _deterministic_manager(limit=2)
    pm2.start_autoplay(b, now=0.0)
    pm2.tick_autoplay(now=0.100)  # seq1 入队
    pm2.tick_autoplay(now=0.110)  # -> blocked
    assert b.autoplay_status == AUTOPLAY_BLOCKED
    with pytest.raises(ChannelCapacityExhausted):
        # blocked 不阻止手动推进；但整批放不下仍按 503 失败，游标不变
        pm2.advance_events(b, 200)
    assert b.cursor_ms == 100


# ---------------------------------------------------------------------------
# 等锁不计时回归：可控时钟 + 线程交错
#
# 修复前启动/恢复在进入会话锁“之前”读取单调时钟：同会话接纳等操作持锁时，
# 控制请求先取时再等待，锁外等待的时长被计入本段计时——生效后游标会额外
# 推进等待时长，并提前入队字幕事件。修复后时钟在会话锁内、状态迁移生效的
# 同一时刻读取。下面用可控时钟与“接纳持锁”的线程交错覆盖：启动、暂停
# 恢复、阻塞恢复与无竞争路径。
# ---------------------------------------------------------------------------


class FakeClock:
    """可控单调时钟：按毫秒步进，由管理器在会话锁内读取。"""

    def __init__(self, now_s=0.0):
        self._now_s = now_s
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._now_s

    def advance_ms(self, ms):
        with self._lock:
            self._now_s += ms / 1000.0


class AcceptHoldingLock:
    """在会话锁内阻塞的接纳：模拟同会话接纳持锁，让控制请求排队等锁。

    accept 的 load_record 回调在会话锁内执行：进入后等待主线程放行，
    持锁期间同会话的启动/恢复请求只能在锁外排队。
    """

    def __init__(self, pm, session, record):
        self._pm = pm
        self._session = session
        self._record = record
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error = None

    def _load(self):
        self.entered.set()
        assert self.release.wait(timeout=5)
        return self._record

    def _run(self):
        try:
            self._pm.accept(self._session, self._load)
        except Exception as exc:  # pragma: no cover - 失败即测试缺陷
            self.error = exc

    def __enter__(self):
        self.thread.start()
        assert self.entered.wait(timeout=5)
        return self

    def __exit__(self, *exc_info):
        self.release.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        assert self.error is None
        return False


def _record_tracks(scene_id, subtitles, tracks):
    """显式指定轨道的场景快照（轨道与字幕按下标对齐）。"""
    subs = [dict(s) for s in subtitles]
    return SceneRecord(
        scene_id=scene_id,
        revision=1,
        track_count=2,
        subtitles=subs,
        assignments=[
            {"id": s["id"], "track": t} for s, t in zip(subs, tracks)
        ],
        changes=0,
    )


def _clock_manager(clock, limit=16):
    return PlaybackManager(
        channel_event_limit=limit,
        autoplay_tick_interval_s=None,
        clock=clock,
    )


def test_start_does_not_count_lock_wait():
    """启动等锁不计时：游标 0、等锁 250ms，生效后 10ms 推进一拍，游标为 10。

    修复前启动在锁外读时钟，250ms 等锁被计入，首拍目标会变成 260 并把
    100ms 字幕的 enter/exit 提前入队。
    """
    clock = FakeClock()
    pm = _clock_manager(clock)
    record = _record_tracks(
        "scene-lock-start",
        [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 5000},
            {"id": "b", "speaker": "B", "start_ms": 100, "end_ms": 200},
        ],
        [1, 2],
    )
    s = _open_session(pm, record)

    started = {}
    with AcceptHoldingLock(pm, s, record):
        worker = threading.Thread(
            target=lambda: started.update(pm.start_autoplay(s)), daemon=True
        )
        worker.start()
        time.sleep(0.05)  # 让启动请求在锁外排队
        clock.advance_ms(250)  # 等锁 250ms：不得计入计时段
    worker.join(timeout=5)
    assert started == {
        "session_id": s.session_id,
        "status": AUTOPLAY_RUNNING,
        "cursor_ms": 0,
    }
    # 计时段从串行生效时刻（等锁之后）起算，而不是从进入锁之前起算
    assert s.base_monotonic_s == clock()

    clock.advance_ms(10)
    pm.tick_autoplay()
    assert s.cursor_ms == 10  # 而非 260
    # 100ms 字幕不得入队、不得消耗 seq
    assert _queued(pm, s) == []
    assert s.channel.next_seq == 1


def test_resume_from_pause_does_not_count_lock_wait():
    """暂停恢复等锁不计时：保留游标 100、等锁 250ms，生效后 +10ms 到 110。"""
    clock = FakeClock()
    pm = _clock_manager(clock)
    record = _record_tracks(
        "scene-lock-pause",
        [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 5000},
            {"id": "b", "speaker": "B", "start_ms": 100, "end_ms": 200},
            {"id": "c", "speaker": "C", "start_ms": 300, "end_ms": 400},
        ],
        [1, 2, 2],
    )
    s = _open_session(pm, record)
    pm.start_autoplay(s)  # 时钟 0：无竞争启动
    clock.advance_ms(100)
    pm.tick_autoplay()  # 游标 100，b enter 入队为 seq1
    assert s.cursor_ms == 100
    assert [e["seq"] for e in _queued(pm, s)] == [1]
    pm.pause_autoplay(s)

    resumed = {}
    with AcceptHoldingLock(pm, s, record):
        worker = threading.Thread(
            target=lambda: resumed.update(pm.resume_autoplay(s)), daemon=True
        )
        worker.start()
        time.sleep(0.05)  # 让恢复请求在锁外排队
        clock.advance_ms(250)  # 等锁 250ms：不得计入新一段
    worker.join(timeout=5)
    assert resumed["status"] == AUTOPLAY_RUNNING
    assert resumed["cursor_ms"] == 100
    assert s.base_monotonic_s == clock()

    clock.advance_ms(10)
    pm.tick_autoplay()
    assert s.cursor_ms == 110  # 而非 360
    # 300ms 的 c 不得提前入队；队列仍只有 seq1，seq 未被消耗
    assert [e["seq"] for e in _queued(pm, s)] == [1]
    assert s.channel.next_seq == 2


def test_resume_from_blocked_does_not_count_lock_wait():
    """阻塞恢复等锁不计时：保留游标 100、等锁 250ms，生效后 +10ms 到 110。"""
    clock = FakeClock()
    pm = _clock_manager(clock, limit=1)
    record = _record_tracks(
        "scene-lock-block",
        [
            {"id": "a", "speaker": "A", "start_ms": 100, "end_ms": 5000},
            {"id": "b", "speaker": "B", "start_ms": 300, "end_ms": 4000},
        ],
        [1, 2],
    )
    s = _open_session(pm, record)
    pm.start_autoplay(s)  # 时钟 0
    clock.advance_ms(100)
    pm.tick_autoplay()  # a enter@100 入队为 seq1，占满容量（limit=1）
    assert s.cursor_ms == 100
    clock.advance_ms(300)
    pm.tick_autoplay()  # 整批 b enter@300 放不下 -> blocked，游标/seq/队列不变
    assert s.autoplay_status == AUTOPLAY_BLOCKED
    assert s.cursor_ms == 100
    assert pm.channel_capacity.used == 1

    # 确认释放容量（先读建立已返回上界，再确认删除前缀）
    assert [e["seq"] for e in _queued(pm, s)] == [1]
    pm.fetch_channel(s, 1)
    assert pm.channel_capacity.used == 0

    resumed = {}
    with AcceptHoldingLock(pm, s, record):
        worker = threading.Thread(
            target=lambda: resumed.update(pm.resume_autoplay(s)), daemon=True
        )
        worker.start()
        time.sleep(0.05)  # 让恢复请求在锁外排队
        clock.advance_ms(250)  # 等锁 250ms：不得计入新一段
    worker.join(timeout=5)
    assert resumed["status"] == AUTOPLAY_RUNNING
    assert s.base_monotonic_s == clock()

    clock.advance_ms(10)
    pm.tick_autoplay()
    assert s.autoplay_status == AUTOPLAY_RUNNING
    assert s.cursor_ms == 110  # 而非 360
    # b@300 未提前入队；容量充足但窗口 (100,110] 内无事件
    assert _queued(pm, s) == []
    assert s.channel.next_seq == 2  # seq 未消耗


def test_start_pause_resume_without_contention_unchanged():
    """无竞争路径：锁内读时钟与既有行为一致；注入 now 仍优先于管理器时钟。"""
    clock = FakeClock()
    pm = _clock_manager(clock)
    record = _record_tracks(
        "scene-lock-free",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 5000}],
        [1],
    )
    s = _open_session(pm, record)
    pm.start_autoplay(s)  # 无等锁：立即读到时钟 0
    assert s.base_monotonic_s == 0.0
    clock.advance_ms(50)
    pm.tick_autoplay()
    assert s.cursor_ms == 50
    pm.pause_autoplay(s)
    clock.advance_ms(1000)  # 暂停不计时
    pm.resume_autoplay(s)  # 无等锁：立即读到当前时钟
    assert s.base_monotonic_s == clock()
    clock.advance_ms(50)
    pm.tick_autoplay()
    assert s.cursor_ms == 100  # 50 + 50，不追赶暂停的 1000ms
    pm.finish(s.session_id)

    # 保留时钟注入：显式 now 优先于管理器时钟（此时受控时钟在 1.1s）
    s2 = _open_session(
        pm,
        _record_tracks(
            "scene-lock-free-2",
            [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 5000}],
            [1],
        ),
    )
    pm.start_autoplay(s2, now=100.0)
    assert s2.base_monotonic_s == 100.0
    pm.tick_autoplay(now=100.5)
    assert s2.cursor_ms == 500


def test_http_start_does_not_count_lock_wait(monkeypatch):
    """HTTP 层面回归：启动请求等锁 250ms 不计入，生效后 10ms 游标为 10。

    受控时钟注入单例管理器；主线程直接持会话锁模拟同会话操作（如进行中
    的接纳），另一线程经 HTTP 发起启动。
    """
    clock = FakeClock()
    pm = _clock_manager(clock)
    monkeypatch.setattr(main_module, "_playback", pm)
    client = TestClient(app)
    sid = make_session(
        client,
        "scene-auto-lockwait",
        subtitles=[
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 5000},
            {"id": "b", "speaker": "B", "start_ms": 100, "end_ms": 200},
        ],
    )
    open_channel(client, sid)
    session = pm.get(sid)

    responses = []
    session.lock.acquire()  # 模拟同会话操作持锁
    try:
        worker = threading.Thread(
            target=lambda: responses.append(
                TestClient(app).post(f"/sessions/{sid}/autoplay/start")
            ),
            daemon=True,
        )
        worker.start()
        time.sleep(0.05)  # 让启动请求在锁外排队
        clock.advance_ms(250)  # 等锁 250ms：不得计入计时段
    finally:
        session.lock.release()
    worker.join(timeout=5)
    assert responses[0].status_code == 200
    assert responses[0].json() == {
        "session_id": sid,
        "status": "running",
        "cursor_ms": 0,
    }

    clock.advance_ms(10)
    pm.tick_autoplay()  # 生效后 10ms 推进一拍
    assert status(client, sid) == {
        "session_id": sid,
        "status": "running",
        "cursor_ms": 10,
    }
    # 100ms 字幕未入队、未消耗 seq
    assert fetch(client, sid, 0).json()["events"] == []
    assert session.channel.next_seq == 1
    client.post(f"/sessions/{sid}/finish")



def test_blocked_resumes_after_capacity_is_available(monkeypatch):
    """上限足够大时：确认释放并显式恢复后，整批成功，事件接续旧 seq。"""
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "32")
    main_module.reset_playback()
    client = TestClient(app)
    sid = make_session(
        client, "scene-auto-unblock", subtitles=_two_batch_subtitles()
    )
    open_channel(client, sid)
    start(client, sid)
    # 全部 10 条事件最终都入队（2 + 8），等待游标越过末事件时刻
    wait_cursor_at_least(client, sid, 130)
    queued = fetch(client, sid, 0).json()["events"]
    assert [e["seq"] for e in queued] == list(range(1, 11))
    # 首批 2 条早于第二批 8 条：时刻排序成立
    assert all(
        queued[i]["time_ms"] <= queued[i + 1]["time_ms"]
        for i in range(len(queued) - 1)
    )
    client.post(f"/sessions/{sid}/finish")


def test_resume_from_blocked_reblocks_without_cursor_change(monkeypatch):
    """阻塞恢复时容量仍不足：下一拍再次阻塞，游标不动。"""
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "3")
    main_module.reset_playback()
    client = TestClient(app)
    sid = make_session(
        client, "scene-auto-reblock", subtitles=_two_batch_subtitles()
    )
    open_channel(client, sid)
    start(client, sid)
    body = wait_status(client, sid, "blocked")
    blocked_cursor = body["cursor_ms"]
    # 不确认释放：直接恢复，容量仍不足以下一批 8 条整批
    resume(client, sid)
    body = wait_status(client, sid, "blocked")
    assert body["cursor_ms"] == blocked_cursor
    client.post(f"/sessions/{sid}/finish")


# ---------------------------------------------------------------------------
# 结束：停止驱动、释放容量、后续 404
# ---------------------------------------------------------------------------


def test_finish_while_running_stops_driver_and_404_afterwards():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-finish")
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 50)
    resp = client.post(f"/sessions/{sid}/finish")
    assert resp.status_code == 200
    # 结束后所有自动播放控制（含查询）与既有操作均 404
    for call in (
        lambda: client.get(f"/sessions/{sid}/autoplay"),
        lambda: client.post(f"/sessions/{sid}/autoplay/pause"),
        lambda: client.post(f"/sessions/{sid}/advance", json={"time_ms": 1}),
        lambda: client.post(f"/sessions/{sid}/finish"),
    ):
        assert call().status_code == 404


def test_finish_releases_channel_capacity_consumed_by_autoplay(monkeypatch):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "10")
    main_module.reset_playback()
    client = TestClient(app)
    sid = make_session(client, "scene-auto-finrel")
    open_channel(client, sid)
    start(client, sid)
    wait_cursor_at_least(client, sid, 1001)  # s2 enter、s1 exit、s3 enter
    cap = main_module.get_playback().channel_capacity
    assert cap.used >= 1
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert cap.used == 0


def test_paused_then_finish_and_new_session_unaffected():
    client = TestClient(app)
    a = make_session(client, "scene-auto-multi-a")
    open_channel(client, a)
    start(client, a)
    wait_cursor_at_least(client, a, 30)
    pause(client, a)
    # 另一个会话同时自动推进
    b = make_session(client, "scene-auto-multi-b")
    open_channel(client, b)
    start(client, b)
    wait_cursor_at_least(client, b, 30)
    # 结束 A 不影响 B 的驱动
    assert client.post(f"/sessions/{a}/finish").status_code == 200
    b_before = status(client, b)["cursor_ms"]
    wait_for(lambda: status(client, b)["cursor_ms"] > b_before)
    client.post(f"/sessions/{b}/finish")


# ---------------------------------------------------------------------------
# 驱动线程：reset 退役旧管理器时停止；旧响应数据不被后台改写
# ---------------------------------------------------------------------------


def test_reset_playback_stops_driver_thread():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-thread")
    open_channel(client, sid)
    start(client, sid)
    manager = main_module.get_playback()
    thread = manager._driver_thread
    assert thread is not None and thread.is_alive()
    main_module.reset_playback()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_old_response_payload_not_mutated_by_background_ticks():
    client = TestClient(app)
    sid = make_session(client, "scene-auto-payload")
    open_channel(client, sid)
    snapshot = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 10}
    ).json()
    start(client, sid)
    wait_cursor_at_least(client, sid, 200)
    # 之前返回的响应体是独立拷贝，游标字段保持 10
    assert snapshot["cursor_ms"] == 10
    client.post(f"/sessions/{sid}/finish")


def test_concurrent_controls_serialize_without_error():
    """启动/暂停/恢复与查询、补取高并发交错：不报错、状态始终合法。"""
    client = TestClient(app)
    sid = make_session(client, "scene-auto-conc")
    open_channel(client, sid)
    allowed = {200, 404, 409}
    stop = threading.Event()
    codes = []

    def worker(fn):
        local = TestClient(app)
        while not stop.is_set():
            codes.append(fn(local).status_code)

    threads = [
        threading.Thread(
            target=worker,
            args=(lambda c: c.post(f"/sessions/{sid}/autoplay/pause"),),
        ),
        threading.Thread(
            target=worker,
            args=(lambda c: c.post(f"/sessions/{sid}/autoplay/resume"),),
        ),
        threading.Thread(
            target=worker,
            args=(lambda c: c.get(f"/sessions/{sid}/autoplay"),),
        ),
        threading.Thread(
            target=worker,
            args=(lambda c: c.post(
                f"/sessions/{sid}/channel/fetch", json={"ack_seq": 0}),),
        ),
    ]
    # 先启动
    start(client, sid)
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert codes, "并发期间应产生过响应"
    assert set(codes) <= allowed, set(codes)
    assert status(client, sid)["status"] in (
        "running", "paused", "blocked"
    )
    client.post(f"/sessions/{sid}/finish")
