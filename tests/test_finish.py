"""播放会话结束（finish）测试。

覆盖：
- 成功响应沿用创建会话的全部字段，快照取自轮到结束时的会话（含游标），
  不读取尚未接纳的场景修订；
- 未知会话 404；结束后针对该 id 的一切请求（advance/events/accept/finish）
  均 404，包括已取得会话引用但尚未生效的在途请求；
- 并发结束最多一次成功；
- 不改动源场景、其他会话或此前返回的快照；
- 用可控交错覆盖等待中的推进、事件推进、接纳与重复结束；
- 不保存结束记录、不跨重启（内存态，重启即无）。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.playback import SessionFinished, SessionMissing  # noqa: E402


def scene_payload(track_count=2):
    """s1[0,1000) s2[800,3000)锁2 s3[1000,2500) —— 分轨 1/2/1，换轨 0。"""
    return {
        "track_count": track_count,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ],
    }


def revise(client, scene_id, expected_revision, subtitles, track_count=2):
    resp = client.put(f"/scenes/{scene_id}", json={
        "track_count": track_count,
        "expected_revision": expected_revision,
        "subtitles": subtitles,
    })
    assert resp.status_code == 200
    assert resp.json()["feasible"] is True
    return resp.json()


def make_scene_session(client, scene_id="scene-finish", advance_to=None):
    """创建 revision=1 场景并以其创建会话；可选地先推进游标。"""
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    created = resp.json()
    if advance_to is not None:
        resp = client.post(
            f"/sessions/{created['session_id']}/advance",
            json={"time_ms": advance_to},
        )
        assert resp.status_code == 200
    return created


def _statuses_after_finish(client, sid):
    """结束后针对该 id 的各类有效请求一律 404。"""
    return {
        "advance": client.post(
            f"/sessions/{sid}/advance", json={"time_ms": 0}
        ).status_code,
        "events": client.post(
            f"/sessions/{sid}/events", json={"time_ms": 0}
        ).status_code,
        "accept": client.post(f"/sessions/{sid}/accept").status_code,
        "finish": client.post(f"/sessions/{sid}/finish").status_code,
    }


# ---------------------------------------------------------------------------
# 基本行为
# ---------------------------------------------------------------------------


def test_finish_returns_full_creation_fields_with_final_cursor_and_snapshot():
    client = TestClient(app)
    created = make_scene_session(client, "scene-finish-basic", advance_to=900)
    sid = created["session_id"]

    resp = client.post(f"/sessions/{sid}/finish")
    assert resp.status_code == 200
    body = resp.json()

    # 成功响应沿用创建会话的全部字段
    assert set(body.keys()) == set(created.keys())
    assert body["session_id"] == sid
    assert body["scene_id"] == "scene-finish-basic"
    assert body["source_revision"] == 1
    assert body["cursor_ms"] == 900  # 快照取自轮到结束时的会话游标
    assert body["track_count"] == 2
    assert body["changes"] == 0
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3"]
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]

    # 结束后该 id 的一切请求均 404
    assert all(code == 404 for code in _statuses_after_finish(client, sid).values())


def test_finish_without_advance_snapshot_has_cursor_zero():
    client = TestClient(app)
    created = make_scene_session(client, "scene-finish-zero")
    sid = created["session_id"]

    body = client.post(f"/sessions/{sid}/finish").json()
    assert body["cursor_ms"] == 0
    assert body["source_revision"] == 1
    assert body["subtitles"] == created["subtitles"]
    assert body["assignments"] == created["assignments"]


def test_finish_unknown_session_404():
    client = TestClient(app)
    resp = client.post("/sessions/no-such-session/finish")
    assert resp.status_code == 404


def test_finish_does_not_read_unaccepted_scene_revisions():
    """结束快照来自会话内存状态：场景修订未接纳时不得出现在快照中。"""
    client = TestClient(app)
    scene_id = "scene-finish-unaccepted"
    sid = make_scene_session(client, scene_id, advance_to=900)["session_id"]

    # 场景推进到 revision 2（s3 延长到 4000，新增 s4），但会话不接纳
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])

    resp = client.post(f"/sessions/{sid}/finish")
    assert resp.status_code == 200
    body = resp.json()
    # 仍是会话接纳的 revision 1 快照：无 s4，s3 的 end_ms 仍为 2500
    assert body["source_revision"] == 1
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3"]
    assert [s["end_ms"] for s in body["subtitles"] if s["id"] == "s3"] == [2500]
    assert body["cursor_ms"] == 900


def test_finish_snapshot_after_accepted_revision():
    """接纳之后再结束：快照取自接纳后的会话（source_revision 跟随接纳）。"""
    client = TestClient(app)
    scene_id = "scene-finish-accepted"
    sid = make_scene_session(client, scene_id, advance_to=900)["session_id"]
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
    ])
    assert client.post(f"/sessions/{sid}/accept").status_code == 200

    body = client.post(f"/sessions/{sid}/finish").json()
    assert body["source_revision"] == 2
    assert [s["end_ms"] for s in body["subtitles"] if s["id"] == "s3"] == [4000]
    assert body["cursor_ms"] == 900


def test_finish_leaves_source_scene_other_sessions_and_prior_snapshots():
    client = TestClient(app)
    scene_id = "scene-finish-isolate"
    finished_req = make_scene_session(client, scene_id, advance_to=900)
    sid_a = finished_req["session_id"]
    # 同场景的另一个会话
    other = client.post("/sessions", json={"scene_id": scene_id}).json()
    sid_b = other["session_id"]

    resp = client.post(f"/sessions/{sid_a}/finish")
    assert resp.status_code == 200
    finished = resp.json()

    # 源场景不变
    scene = client.get(f"/scenes/{scene_id}").json()
    assert scene["revision"] == 1
    assert [s["id"] for s in scene["subtitles"]] == ["s1", "s2", "s3"]

    # 其他会话不受影响，可继续推进/结束
    body = client.post(
        f"/sessions/{sid_b}/advance", json={"time_ms": 1000}
    ).json()
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s2", 2),
        ("s3", 1),
    ]
    assert client.post(f"/sessions/{sid_b}/finish").status_code == 200

    # 此前返回给客户端的快照不被改动（响应体是独立拷贝）
    assert finished["cursor_ms"] == 900
    assert [a["track"] for a in finished["assignments"]] == [1, 2, 1]


def test_finish_allows_new_session_for_same_scene_afterwards():
    """结束只释放会话本身：场景仍在，可基于它再创建新会话。"""
    client = TestClient(app)
    scene_id = "scene-finish-recreate"
    sid = make_scene_session(client, scene_id, advance_to=500)["session_id"]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200

    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    new_state = resp.json()
    assert new_state["session_id"] != sid
    assert new_state["cursor_ms"] == 0  # 全新会话，游标从头开始
    assert new_state["source_revision"] == 1


# ---------------------------------------------------------------------------
# 并发结束：最多一次成功
# ---------------------------------------------------------------------------


def test_concurrent_finishes_at_most_one_success():
    client = TestClient(app)
    sid = make_scene_session(client, "scene-finish-race")["session_id"]

    results = []
    barrier = threading.Barrier(8)

    def worker():
        local = TestClient(app)
        barrier.wait()
        results.append(local.post(f"/sessions/{sid}/finish"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in results)
    assert statuses.count(200) == 1
    assert statuses.count(404) == 7

    winner = next(r for r in results if r.status_code == 200).json()
    assert set(winner.keys()) == {
        "session_id", "scene_id", "source_revision", "cursor_ms",
        "track_count", "changes", "subtitles", "assignments",
    }
    assert winner["session_id"] == sid
    # 结束后注册表无条目
    assert all(code == 404 for code in _statuses_after_finish(client, sid).values())


# ---------------------------------------------------------------------------
# 可控交错：等待中的推进 / 事件推进 / 接纳 / 重复结束
# ---------------------------------------------------------------------------


def _patch_session_active_gate(monkeypatch):
    """让每个会话的 active() 第一次调用在会话锁内阻塞。

    active() 只在 advance/advance_events 持有会话锁时调用；借此稳定制造
    “请求已进入会话锁、结束正在锁上等待”的交错。
    返回 (已进入事件, 放行事件)。
    """
    from app.playback import PlaybackSession

    entered = threading.Event()
    release = threading.Event()
    gated = set()
    original_active = PlaybackSession.active

    def gated_active(self):
        if id(self) not in gated:
            gated.add(id(self))
            entered.set()
            assert release.wait(timeout=10), "测试未能及时放行被阻塞的操作"
        return original_active(self)

    monkeypatch.setattr(PlaybackSession, "active", gated_active)
    return entered, release


def test_waiting_advance_linearized_before_finish_then_404(monkeypatch):
    """推进先持锁（游标推进发生在 active 之前）→ finish 等待 →
    推进返回 200 且其游标进入最终快照；之后针对该 id 的请求均 404。"""
    client = TestClient(app)
    sid = make_scene_session(client, "scene-finish-adv")["session_id"]

    entered, release = _patch_session_active_gate(monkeypatch)
    responses = {}

    def do_advance():
        local = TestClient(app)
        responses["advance"] = local.post(
            f"/sessions/{sid}/advance", json={"time_ms": 900}
        )

    t_adv = threading.Thread(target=do_advance)
    t_adv.start()
    assert entered.wait(timeout=10), "推进未进入会话锁"

    def do_finish():
        local = TestClient(app)
        responses["finish"] = local.post(f"/sessions/{sid}/finish")

    t_fin = threading.Thread(target=do_finish)
    t_fin.start()
    try:
        t_fin.join(timeout=0.5)
        assert t_fin.is_alive(), "finish 不应在持锁推进完成前生效"
        # 注册表条目仍在（finish 尚未进入其临界区移除条目）
        assert main_module.get_playback().get(sid) is not None
    finally:
        release.set()
    t_adv.join(timeout=10)
    t_fin.join(timeout=10)
    assert not t_adv.is_alive() and not t_fin.is_alive()

    assert responses["advance"].status_code == 200
    adv = responses["advance"].json()
    assert adv["cursor_ms"] == 900
    assert [(a["id"], a["track"]) for a in adv["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]

    assert responses["finish"].status_code == 200
    final = responses["finish"].json()
    # 最终快照包含串行在它之前的那次推进
    assert final["cursor_ms"] == 900
    assert all(code == 404 for code in _statuses_after_finish(client, sid).values())


def test_waiting_events_linearized_before_finish(monkeypatch):
    """事件推进先持锁 → finish 等待：事件正常返回，最终游标为事件推进游标。"""
    client = TestClient(app)
    sid = make_scene_session(client, "scene-finish-evt")["session_id"]

    entered, release = _patch_session_active_gate(monkeypatch)
    responses = {}

    def do_events():
        local = TestClient(app)
        responses["events"] = local.post(
            f"/sessions/{sid}/events", json={"time_ms": 3000}
        )

    t_evt = threading.Thread(target=do_events)
    t_evt.start()
    assert entered.wait(timeout=10)

    def do_finish():
        local = TestClient(app)
        responses["finish"] = local.post(f"/sessions/{sid}/finish")

    t_fin = threading.Thread(target=do_finish)
    t_fin.start()
    try:
        t_fin.join(timeout=0.5)
        assert t_fin.is_alive()
    finally:
        release.set()
    t_evt.join(timeout=10)
    t_fin.join(timeout=10)

    assert responses["events"].status_code == 200
    evt = responses["events"].json()
    assert evt["cursor_ms"] == 3000
    assert evt["active"] == []
    assert responses["finish"].status_code == 200
    assert responses["finish"].json()["cursor_ms"] == 3000


def test_finish_then_cached_reference_requests_all_get_404(monkeypatch):
    """确定性交错（管理器层）：先取得会话引用但未取锁，finish 先生效，
    随后该引用上的推进/事件推进/接纳/重复结束全部按结束会话处理。"""
    client = TestClient(app)
    sid = make_scene_session(
        client, "scene-finish-cached-ref", advance_to=500
    )["session_id"]
    manager = main_module.get_playback()
    session = manager.get(sid)
    assert session is not None

    # finish 先生效（注册表移除 + 墓碑置位）
    final = manager.finish(sid)
    assert final["cursor_ms"] == 500

    # 管理器层：持引用的操作在会话锁内看到墓碑
    with pytest.raises(SessionFinished):
        manager.advance(session, 600)
    with pytest.raises(SessionFinished):
        manager.advance_events(session, 600)
    with pytest.raises(SessionFinished):
        # load_record 不应被调用：墓碑检查先于场景读取
        manager.accept(session, lambda: pytest.fail("结束后不得读取场景"))
    with pytest.raises(SessionMissing):
        manager.finish(sid)

    # HTTP 层：模拟路由 get 到已置墓碑的在途引用，四个端点一致 404
    monkeypatch.setattr(manager, "get", lambda _sid: session)
    local = TestClient(app)
    assert local.post(
        f"/sessions/{sid}/advance", json={"time_ms": 600}
    ).status_code == 404
    assert local.post(
        f"/sessions/{sid}/events", json={"time_ms": 600}
    ).status_code == 404
    assert local.post(f"/sessions/{sid}/accept").status_code == 404
    assert local.post(f"/sessions/{sid}/finish").status_code == 404


def _patch_first_scene_read(monkeypatch):
    """让第一次 store.get_scene 在会话锁内（accept 读取处）暂停。

    返回 (已暂停事件, 放行事件)：模拟“接纳已进入完整操作、正持会话锁
    读取场景”的交错，此时结束必须排在接纳之后。
    """
    store = main_module.get_store()
    real_get_scene = store.get_scene
    paused = threading.Event()
    release = threading.Event()
    injected = []

    def wrapped_get_scene(scene_id):
        if injected:
            return real_get_scene(scene_id)
        injected.append(scene_id)
        paused.set()
        assert release.wait(timeout=10), "测试未能及时放行被暂停的接纳"
        return real_get_scene(scene_id)

    monkeypatch.setattr(store, "get_scene", wrapped_get_scene)
    return paused, release


def test_waiting_accept_linearized_before_finish_with_duplicate_finish(monkeypatch):
    """接纳先持会话锁（在锁内读取场景处暂停）→ 两个结束等待：
    场景修订在接纳暂停期间落库，接纳轮到读取时纳入该修订；结束排在
    接纳之后，最多一个结束成功且最终快照即接纳后的快照，另一个 404。"""
    client = TestClient(app)
    scene_id = "scene-finish-accept"
    sid = make_scene_session(client, scene_id, advance_to=900)["session_id"]

    paused, release = _patch_first_scene_read(monkeypatch)
    responses = {}

    def do_accept():
        local = TestClient(app)
        responses["accept"] = local.post(f"/sessions/{sid}/accept")

    t_acc = threading.Thread(target=do_accept)
    t_acc.start()
    assert paused.wait(timeout=10), "接纳未进入会话锁内的场景读取"

    # 接纳持锁暂停期间，场景修订到 revision 2（接纳在锁内的读取尚未发生）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
    ])

    def post_finish(key):
        local = TestClient(app)
        responses[key] = local.post(f"/sessions/{sid}/finish")

    t_fin = threading.Thread(target=post_finish, args=("finish",))
    t_fin2 = threading.Thread(target=post_finish, args=("finish_again",))
    t_fin.start()
    t_fin2.start()
    try:
        t_fin.join(timeout=0.5)
        assert t_fin.is_alive(), "finish 不应在持锁接纳完成前生效"
        t_fin2.join(timeout=0.3)
        assert t_fin2.is_alive(), "重复结束同样必须等接纳先完成"
    finally:
        release.set()
    t_acc.join(timeout=10)
    t_fin.join(timeout=10)
    t_fin2.join(timeout=10)
    assert not (t_acc.is_alive() or t_fin.is_alive() or t_fin2.is_alive())

    # 接纳在其完整操作内读到 revision 2 并成功
    assert responses["accept"].status_code == 200
    assert responses["accept"].json()["source_revision"] == 2

    # 两个结束中最多一个成功，且成功快照取自接纳后的会话
    finishes = [responses["finish"], responses["finish_again"]]
    ok = [r for r in finishes if r.status_code == 200]
    gone = [r for r in finishes if r.status_code == 404]
    assert len(ok) == 1 and len(gone) == 1
    final = ok[0].json()
    assert final["source_revision"] == 2
    assert [s["end_ms"] for s in final["subtitles"] if s["id"] == "s3"] == [4000]
    assert final["cursor_ms"] == 900

    # 接纳之后针对该 id 再发的请求一律 404
    assert all(code == 404 for code in _statuses_after_finish(client, sid).values())


def test_finish_first_then_accept_and_duplicate_finish_all_404():
    """finish 先生效后，紧接到达的接纳与重复结束均 404；
    失败的接纳不改变源场景（场景修订是独立的外部操作）。"""
    client = TestClient(app)
    scene_id = "scene-finish-first"
    sid = make_scene_session(client, scene_id, advance_to=900)["session_id"]

    assert client.post(f"/sessions/{sid}/finish").status_code == 200

    # 场景即便在此期间被修订，结束后的接纳也不会执行（更不会被会话接纳）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
    ])
    assert _statuses_after_finish(client, sid) == {
        "advance": 404, "events": 404, "accept": 404, "finish": 404
    }

    scene = client.get(f"/scenes/{scene_id}").json()
    assert scene["revision"] == 2  # 场景修订照常落库，只是不被已结束会话接纳


# ---------------------------------------------------------------------------
# 不保存结束记录 / 不跨重启：会话注册表为内存态
# ---------------------------------------------------------------------------


def test_finish_not_persisted_registry_cleared():
    """结束不保存记录：注册表无残留；重置管理器（重启语义）后 id 仍未知，
    而源场景仍可读取。"""
    client = TestClient(app)
    sid = make_scene_session(client, "scene-finish-norec", advance_to=100)[
        "session_id"
    ]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert client.post(f"/sessions/{sid}/finish").status_code == 404

    assert main_module.get_playback()._sessions == {}
    main_module.reset_playback()
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 0}
    ).status_code == 404
    assert client.get("/scenes/scene-finish-norec").status_code == 200


def test_finish_response_is_independent_copy():
    """结束响应在置墓碑后由 state() 复制字幕/分轨生成：注册表不再可达，
    已返回的最终快照不受任何后续操作影响。"""
    client = TestClient(app)
    sid = make_scene_session(client, "scene-finish-copy", advance_to=900)[
        "session_id"
    ]
    final = client.post(f"/sessions/{sid}/finish").json()

    assert final["cursor_ms"] == 900
    assert [s["id"] for s in final["subtitles"]] == ["s1", "s2", "s3"]
    assert final["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]
    assert main_module.get_playback().get(sid) is None
