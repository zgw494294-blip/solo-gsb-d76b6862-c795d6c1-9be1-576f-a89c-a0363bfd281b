"""播放存档（checkpoint）：跨重启续播，保留未确认显示事件。

覆盖：
- 保存（PUT/POST /sessions/{id}/checkpoints/{cp_id}、集合式 POST）：
  仅已开补取通道且自动播放非运行（idle/paused/blocked）可保存；
  未开通道或 running 返回 409；成功不结束会话、不改游标/隐藏/队列；
  同名存档 409（先存者保留、失败不留存档）；未知/已结束会话 404；
  非法 id（空/纯空白）422；存档含完整快照、游标、隐藏集合、通道事件
  与 next_seq/acked_seq/max_returned_seq；与同会话操作串行。
- 恢复（POST /checkpoints/{cp_id}/restore 及其别名）：
  按存档恢复为全新 session_id 的独立会话，不读取当前场景（场景删除、
  修订均不影响）；事件、序号、确认边界保留，新事件接着 next_seq 编号；
  未启动仍未启动，paused/blocked 恢复为 paused，resume 重新计时、
  不追赶离线时间；同存档可重复恢复为相互独立会话，后续操作不改存档
  或其他会话；响应含新 id、完整快照与播放状态。
- 容量：恢复队列计入共享容量，不足 503 且不留会话或容量占用；
  并发恢复合计不超额；同名存档并发保存恰好一次成功。
- 持久化：reset_playback（模拟进程重启）后原会话 404，存档仍可恢复；
  存档落在 SCENES_DB_PATH 的同一 SQLite。
- 旧接口响应不变。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402


def scene_payload(scene_id="scene-cp", subtitles=None, track_count=2,
                  expected_revision=0):
    if subtitles is None:
        # s1[0,1000) s2[800,3000)锁2 s3[1000,2500) —— 分轨 1/2/1，换轨 0
        subtitles = [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ]
    return {
        "track_count": track_count,
        "expected_revision": expected_revision,
        "subtitles": subtitles,
    }


def make_client():
    return TestClient(app)


def create_session(client, scene_id="scene-cp", payload=None):
    client.put(f"/scenes/{scene_id}", json=payload or scene_payload(scene_id))
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def save_cp(client, sid, cp_id, method="put", collection=False, json_body=None):
    if collection:
        return client.post(
            f"/sessions/{sid}/checkpoints",
            json={"checkpoint_id": cp_id} if json_body is None else json_body,
        )
    func = client.put if method == "put" else client.post
    return func(f"/sessions/{sid}/checkpoints/{cp_id}")


def restore(client, cp_id, alias=False):
    prefix = "/sessions/checkpoints" if alias else "/checkpoints"
    return client.post(f"{prefix}/{cp_id}/restore")


def fetch(client, sid, ack_seq):
    return client.post(
        f"/sessions/{sid}/channel/fetch", json={"ack_seq": ack_seq}
    )


def events(client, sid, t):
    return client.post(f"/sessions/{sid}/events", json={"time_ms": t})


def prep_session_with_queued_events(client, scene_id="scene-cp"):
    """建场景/会话/通道，推进到 1000，返回 (sid, 入队事件四元组列表)。"""
    sid = create_session(client, scene_id)
    assert client.post(f"/sessions/{sid}/channel").status_code == 200
    resp = events(client, sid, 1000)
    assert resp.status_code == 200
    assert [(e["time_ms"], e["type"], e["id"]) for e in resp.json()["events"]] == [
        (800, "enter", "s2"),
        (1000, "exit", "s1"),
        (1000, "enter", "s3"),
    ]
    return sid


# ---------------------------------------------------------------------------
# 保存：前置条件（已开通道且非 running）
# ---------------------------------------------------------------------------


def test_save_requires_channel_409():
    client = make_client()
    sid = create_session(client, "scene-cp-nochan")
    resp = save_cp(client, sid, "cp-1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "channel_missing"
    # 会话完全不受影响：开启通道后可正常存档
    assert client.post(f"/sessions/{sid}/channel").status_code == 200
    assert save_cp(client, sid, "cp-1").status_code == 200


def test_save_running_autoplay_409_but_paused_and_blocked_allowed():
    client = make_client()
    sid = create_session(client, "scene-cp-run")
    client.post(f"/sessions/{sid}/channel")
    client.post(f"/sessions/{sid}/autoplay/start")
    resp = save_cp(client, sid, "cp-run")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "autoplay_running"
    # 暂停后可以存档，响应状态为 paused
    client.post(f"/sessions/{sid}/autoplay/pause")
    resp = save_cp(client, sid, "cp-paused")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    assert resp.json()["autoplay_status"] == "paused"


def test_save_blocked_session_allowed(monkeypatch):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "3")
    main_module.reset_playback()
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-blk")
    pb = main_module.get_playback()
    session = pb.get(sid)
    client.post(f"/sessions/{sid}/autoplay/start")
    # 容量已满（3）：推进到 2500 的整拍（1 条 s3 exit）整批失败 -> blocked
    pb.tick_autoplay(now=pb._clock() + 2.0)
    assert client.get(f"/sessions/{sid}/autoplay").json()["status"] == "blocked"
    resp = save_cp(client, sid, "cp-blocked")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["cursor_ms"] == 1000  # 阻塞拍未推进游标
    assert body["queued_events"] == 3
    assert body["next_seq"] == 4
    assert body["max_returned_seq"] == 0  # 从未补取过


def test_save_success_does_not_finish_or_mutate_session():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-keep")
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )
    fetch(client, sid, 0)
    resp = save_cp(client, sid, "cp-keep")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["cursor_ms"] == 1000
    assert body["next_seq"] == 5  # 3 条推进事件 + 隐藏 s3 的 exit
    assert body["acked_seq"] == 0
    assert body["max_returned_seq"] == 4
    assert body["queued_events"] == 4

    # 存档后会话仍可继续操作（未结束），游标可继续推进
    resp = events(client, sid, 3000)
    assert resp.status_code == 200
    assert resp.json()["cursor_ms"] == 3000
    # 存档可重复保存为不同 id（每次序列化当前状态）
    assert save_cp(client, sid, "cp-keep-2").status_code == 200
    # 会话最后仍能正常结束
    assert client.post(f"/sessions/{sid}/finish").status_code == 200


def test_save_finished_session_404():
    client = make_client()
    sid = create_session(client, "scene-cp-fin")
    client.post(f"/sessions/{sid}/channel")
    client.post(f"/sessions/{sid}/finish")
    assert save_cp(client, sid, "cp-after-finish").status_code == 404


def test_save_unknown_session_404():
    client = make_client()
    assert save_cp(client, "no-such-session", "cp-x").status_code == 404


@pytest.mark.parametrize("bad_id", ["", "   ", "\t", " \t "])
def test_save_invalid_checkpoint_id_422(bad_id):
    client = make_client()
    sid = create_session(client, "scene-cp-bad-id")
    client.post(f"/sessions/{sid}/channel")
    # 路径无法表达空 id，空/纯空白 id 经集合式请求体按 422 拒绝；
    # 路径形式的纯空白 id（%20...）在下面的恢复用例中覆盖。
    resp = client.post(
        f"/sessions/{sid}/checkpoints", json={"checkpoint_id": bad_id}
    )
    assert resp.status_code == 422


def test_save_whitespace_path_id_422():
    client = make_client()
    sid = create_session(client, "scene-cp-bad-path")
    client.post(f"/sessions/{sid}/channel")
    resp = client.put(f"/sessions/{sid}/checkpoints/%20%20")
    assert resp.status_code == 422


def test_save_collection_style_post():
    client = make_client()
    sid = create_session(client, "scene-cp-collection")
    client.post(f"/sessions/{sid}/channel")
    resp = client.post(
        f"/sessions/{sid}/checkpoints", json={"checkpoint_id": "cp-coll"}
    )
    assert resp.status_code == 200
    assert resp.json()["checkpoint_id"] == "cp-coll"
    # 集合式缺少字段 -> 422
    resp = client.post(f"/sessions/{sid}/checkpoints", json={})
    assert resp.status_code == 422


def test_save_post_path_alias_equivalent_to_put():
    client = make_client()
    sid = create_session(client, "scene-cp-post")
    client.post(f"/sessions/{sid}/channel")
    assert save_cp(client, sid, "cp-post", method="post").status_code == 200
    # PUT 同名再存 -> 409
    assert save_cp(client, sid, "cp-post", method="put").status_code == 409


# ---------------------------------------------------------------------------
# 同名存档 409：先存者保留、失败不留存档
# ---------------------------------------------------------------------------


def test_save_duplicate_checkpoint_id_409():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-dup")
    assert save_cp(client, sid, "cp-dup").status_code == 200
    resp = save_cp(client, sid, "cp-dup")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["checkpoint_id"] == "cp-dup"
    # 存档内容是第一次的：恢复后游标为 1000（而非推进后的 3000）
    events(client, sid, 3000)
    body = restore(client, "cp-dup").json()
    assert body["cursor_ms"] == 1000


def test_concurrent_same_name_save_succeeds_once():
    client = make_client()
    sid = create_session(client, "scene-cp-duprace")
    client.post(f"/sessions/{sid}/channel")
    codes = []
    lock = threading.Lock()

    def worker():
        c = make_client()
        resp = save_cp(c, sid, "cp-duprace")
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 7


# ---------------------------------------------------------------------------
# 存档内容：快照/游标/隐藏集合/通道事件与序号边界
# ---------------------------------------------------------------------------


def test_checkpoint_persisted_to_scene_sqlite():
    """存档与场景写入同一 SQLite（SCENES_DB_PATH），可直接从库里读到。"""
    import os
    import sqlite3

    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-db")
    assert save_cp(client, sid, "cp-db").status_code == 200
    db_path = os.environ["SCENES_DB_PATH"]
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT session_id, payload FROM playback_checkpoints "
            "WHERE checkpoint_id = ?",
            ("cp-db",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == sid
    import json

    payload = json.loads(row[1])
    assert payload["cursor_ms"] == 1000
    assert [s["id"] for s in payload["subtitles"]] == ["s1", "s2", "s3"]
    assert payload["channel"]["next_seq"] == 4
    assert payload["channel"]["acked_seq"] == 0
    assert [e["seq"] for e in payload["channel"]["events"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 恢复：新 session_id、不读场景、快照/事件/序号/确认边界保留
# ---------------------------------------------------------------------------


def test_restore_returns_new_session_with_snapshot_and_status():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-restore")
    client.post(
        f"/sessions/{sid}/hidden_speakers", json={"hidden_speakers": ["Alice"]}
    )
    save_cp(client, sid, "cp-restore")
    body = restore(client, "cp-restore").json()
    assert body["session_id"] != sid
    assert body["checkpoint_id"] == "cp-restore"
    assert body["source_session_id"] == sid
    assert body["scene_id"] == "scene-cp-restore"
    assert body["source_revision"] == 1
    assert body["cursor_ms"] == 1000
    assert body["status"] == "idle"
    assert body["autoplay_status"] == "idle"
    assert body["hidden_speakers"] == ["Alice"]
    assert body["track_count"] == 2
    assert body["changes"] == 0
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3"]
    assert [(a["id"], a["track"]) for a in body["assignments"]] == [
        ("s1", 1), ("s2", 2), ("s3", 1)
    ]
    channel = body["channel"]
    assert channel["next_seq"] == 5
    assert channel["acked_seq"] == 0
    assert channel["queued_count"] == 4
    assert [(e["seq"], e["type"], e["id"]) for e in channel["events"]] == [
        (1, "enter", "s2"),
        (2, "exit", "s1"),
        (3, "enter", "s3"),
        (4, "exit", "s3"),
    ]


def test_restore_does_not_read_current_scene():
    """恢复只取存档：场景被删除（此处仅修订）不影响恢复内容。"""
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-noread")
    save_cp(client, sid, "cp-noread")
    # 存档后场景修订为 revision 2（s3 延长到 4000）；恢复不得读取它
    resp = client.put(
        "/scenes/scene-cp-noread",
        json=scene_payload(
            "scene-cp-noread",
            subtitles=[
                {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
                {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
                 "locked_track": 2},
                {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
            ],
            expected_revision=1,
        ),
    )
    assert resp.status_code == 200
    body = restore(client, "cp-noread").json()
    assert body["source_revision"] == 1
    s3 = next(s for s in body["subtitles"] if s["id"] == "s3")
    assert s3["end_ms"] == 2500  # 存档时的快照，而非修订后的 4000


def test_restore_unknown_404_and_bad_id_422():
    client = make_client()
    assert restore(client, "no-such-checkpoint").status_code == 404
    resp = client.post("/sessions/checkpoints/%20%20/restore")
    assert resp.status_code == 422


def test_restore_alias_route():
    client = make_client()
    sid = create_session(client, "scene-cp-alias")
    client.post(f"/sessions/{sid}/channel")
    save_cp(client, sid, "cp-alias")
    resp = restore(client, "cp-alias", alias=True)
    assert resp.status_code == 200
    assert resp.json()["session_id"] != sid


def test_restored_queue_seq_and_ack_boundaries_preserved():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-seq")
    # 补取一次建立 max_returned=3，再确认前 2 条（acked=2，剩 seq 3）
    fetch(client, sid, 0)
    fetch(client, sid, 2)
    save_cp(client, sid, "cp-seq")
    body = restore(client, "cp-seq").json()
    nsid = body["session_id"]
    channel = body["channel"]
    assert channel["acked_seq"] == 2
    assert channel["next_seq"] == 4
    assert channel["max_returned_seq"] == 3
    assert [e["seq"] for e in channel["events"]] == [3]

    # 恢复后补取：ack 边界仍是 2；越界按恢复后的 max_returned 判 422
    resp = fetch(client, nsid, 2)
    assert resp.status_code == 200
    assert [e["seq"] for e in resp.json()["events"]] == [3]
    resp = fetch(client, nsid, 99)
    assert resp.status_code == 422
    assert resp.json()["detail"]["max_returned_seq"] == 3

    # 新事件接着存档的 next_seq（=4）连续编号
    resp = events(client, nsid, 3000)
    assert resp.status_code == 200
    resp = fetch(client, nsid, 3)
    seqs = [e["seq"] for e in resp.json()["events"]]
    assert seqs[0] == 4 and seqs == sorted(seqs)


def test_repeated_restores_are_independent():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-repeat")
    save_cp(client, sid, "cp-repeat")
    b1 = restore(client, "cp-repeat").json()
    b2 = restore(client, "cp-repeat").json()
    assert b1["session_id"] != b2["session_id"]
    # 两个恢复会话独立推进、独立确认，互不影响
    events(client, b1["session_id"], 3000)
    resp1 = fetch(client, b1["session_id"], 0)
    resp2 = fetch(client, b2["session_id"], 0)
    # 会话 1 推进到 3000 后补取会看到更多事件；会话 2 仍是存档原样
    assert len(resp1.json()["events"]) > len(resp2.json()["events"])
    assert len(resp2.json()["events"]) == 3
    # 存档本身不变：再次恢复仍从游标 1000、3 条队列开始
    b3 = restore(client, "cp-repeat").json()
    assert b3["cursor_ms"] == 1000
    assert b3["channel"]["queued_count"] == 3


def test_restored_session_ops_do_not_touch_source_or_checkpoint():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-iso")
    save_cp(client, sid, "cp-iso")
    body = restore(client, "cp-iso").json()
    nsid = body["session_id"]
    # 结束恢复出的会话
    assert client.post(f"/sessions/{nsid}/finish").status_code == 200
    # 存档仍可再次恢复；源会话仍在且状态不变
    assert restore(client, "cp-iso").status_code == 200
    resp = events(client, sid, 3000)
    assert resp.status_code == 200
    assert resp.json()["cursor_ms"] == 3000


# ---------------------------------------------------------------------------
# 跨重启：内存清空后存档仍可恢复；旧会话 404
# ---------------------------------------------------------------------------


def test_restore_after_simulated_restart():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-restart")
    fetch(client, sid, 0)
    fetch(client, sid, 1)  # acked=1，剩 seq 2、3
    save_cp(client, sid, "cp-restart")

    # 模拟进程重启：丢弃全部内存会话，但 SQLite（同一 SCENES_DB_PATH）保留
    main_module.reset_playback()
    client2 = make_client()
    # 旧会话 id 已不存在
    assert events(client2, sid, 3000).status_code == 404
    # 存档可恢复
    body = restore(client2, "cp-restart").json()
    assert body["session_id"] != sid
    assert body["cursor_ms"] == 1000
    assert body["channel"]["acked_seq"] == 1
    assert [(e["seq"], e["id"]) for e in body["channel"]["events"]] == [
        (2, "s1"), (3, "s3")
    ]
    # 恢复后可正常补取、推进、结束
    resp = fetch(client2, body["session_id"], 1)
    assert [e["seq"] for e in resp.json()["events"]] == [2, 3]
    resp = events(client2, body["session_id"], 3000)
    assert resp.status_code == 200
    assert client2.post(
        f"/sessions/{body['session_id']}/finish"
    ).status_code == 200


# ---------------------------------------------------------------------------
# 自动播放：idle 仍 idle；paused/blocked 恢复为 paused；resume 重新计时
# ---------------------------------------------------------------------------


def test_restore_idle_stays_idle():
    client = make_client()
    sid = create_session(client, "scene-cp-idle")
    client.post(f"/sessions/{sid}/channel")
    save_cp(client, sid, "cp-idle")
    body = restore(client, "cp-idle").json()
    nsid = body["session_id"]
    assert body["status"] == "idle"
    assert client.get(f"/sessions/{nsid}/autoplay").json()["status"] == "idle"
    # idle 下暂停仍按旧规则 409
    assert client.post(
        f"/sessions/{nsid}/autoplay/pause"
    ).status_code == 409


def test_restore_paused_or_blocked_becomes_paused_and_resume_rebases(monkeypatch):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "3")
    main_module.reset_playback()
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-resume")
    pb = main_module.get_playback()
    client.post(f"/sessions/{sid}/autoplay/start")
    pb.tick_autoplay(now=pb._clock() + 2.0)  # 满额整拍失败 -> blocked
    assert client.get(f"/sessions/{sid}/autoplay").json()["status"] == "blocked"
    save_cp(client, sid, "cp-from-blocked")

    # 模拟重启后恢复（容量初始为 0，但存档 3 条正好等于上限 3）
    main_module.reset_playback()
    client2 = make_client()
    body = restore(client2, "cp-from-blocked").json()
    nsid = body["session_id"]
    assert body["status"] == "paused"
    st = client2.get(f"/sessions/{nsid}/autoplay").json()
    assert st == {"session_id": nsid, "status": "paused", "cursor_ms": 1000}
    # 离线时间不追赶：在整秒/半秒边界（二进制可精确表示）恢复计时，
    # 时钟推进 500ms 后游标恰好只走 500ms，而不是 1000 + 离线很久
    pb2 = main_module.get_playback()
    session = pb2.get(nsid)
    import math

    t0 = math.ceil(pb2._clock()) + 1.0
    pb2.resume_autoplay(session, now=t0)
    pb2.tick_autoplay(now=t0 + 0.5)
    st = client2.get(f"/sessions/{nsid}/autoplay").json()
    assert st["cursor_ms"] == 1500


def test_restore_paused_does_not_start_driver():
    client = make_client()
    sid = create_session(client, "scene-cp-nodrive")
    client.post(f"/sessions/{sid}/channel")
    client.post(f"/sessions/{sid}/autoplay/start")
    client.post(f"/sessions/{sid}/autoplay/pause")
    save_cp(client, sid, "cp-nodrive")
    body = restore(client, "cp-nodrive").json()
    nsid = body["session_id"]
    assert body["status"] == "paused"
    import time

    time.sleep(0.15)  # 即使后台驱动在跑，paused 也不应推进
    st = client.get(f"/sessions/{nsid}/autoplay").json()
    assert st["cursor_ms"] == 0


# ---------------------------------------------------------------------------
# 共享容量：恢复计入，不足 503 且不留会话/占用；并发不超额
# ---------------------------------------------------------------------------


def test_restore_503_when_capacity_insufficient_leaves_nothing(monkeypatch):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "3")
    main_module.reset_playback()
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-cap")
    save_cp(client, sid, "cp-cap")
    pb = main_module.get_playback()
    assert pb.channel_capacity.used == 3  # 源会话队列占满
    before_sessions = len(pb._sessions)
    resp = restore(client, "cp-cap")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["limit"] == 3
    assert detail["requested"] == 3
    assert detail["available"] == 0
    # 不留会话、不留容量占用
    assert pb.channel_capacity.used == 3
    assert len(pb._sessions) == before_sessions
    # 源会话确认释放后即可恢复
    fetch(client, sid, 0)
    fetch(client, sid, 3)
    assert pb.channel_capacity.used == 0
    assert restore(client, "cp-cap").status_code == 200
    assert pb.channel_capacity.used == 3


def test_concurrent_restores_never_exceed_capacity(monkeypatch):
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", "6")
    main_module.reset_playback()
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-caprace")
    save_cp(client, sid, "cp-caprace")
    main_module.reset_playback()  # 重启后只剩存档
    codes = []
    lock = threading.Lock()

    def worker():
        c = make_client()
        resp = restore(c, "cp-caprace")
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pb = main_module.get_playback()
    assert codes.count(200) == 2  # 每个恢复占用 3，上限 6
    assert codes.count(503) == 4
    assert pb.channel_capacity.used == 6
    assert len(pb._sessions) == 2


def test_restore_empty_queue_consumes_no_capacity():
    """存档时通道无未确认事件：恢复不占容量（满额也能恢复）。"""
    client = make_client()
    sid = create_session(client, "scene-cp-emptyq")
    client.post(f"/sessions/{sid}/channel")  # 空队列
    save_cp(client, sid, "cp-emptyq")
    body = restore(client, "cp-emptyq").json()
    assert body["channel"]["queued_count"] == 0
    assert body["channel"]["next_seq"] == 1
    assert main_module.get_playback().channel_capacity.used == 0


# ---------------------------------------------------------------------------
# 与同会话操作串行：存档期间的推进必须完整发生在存档之前或之后
# ---------------------------------------------------------------------------


def test_save_serialized_with_session_operations():
    client = make_client()
    sid = prep_session_with_queued_events(client, "scene-cp-serial")
    # 存档与推进并发交错：存档游标只可能是 1000 或 3000，不会是中间状态；
    # 存档后再推进，队列序号连续。
    outcomes = []
    lock = threading.Lock()

    def do_save(i):
        c = make_client()
        resp = save_cp(c, sid, f"cp-serial-{i}")
        with lock:
            outcomes.append((resp.json()["cursor_ms"], resp.json()["next_seq"]))

    def do_advance():
        c = make_client()
        events(c, sid, 3000)

    threads = [threading.Thread(target=do_advance)]
    threads += [threading.Thread(target=do_save, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for cursor, next_seq in outcomes:
        assert cursor in (1000, 3000)
        # 游标 1000 时 next_seq=4（3 条事件）；游标 3000 时多出 s3、s2 退出
        if cursor == 1000:
            assert next_seq == 4
        else:
            assert next_seq == 6
    # 所有存档都可恢复，内容自洽
    for i in range(4):
        body = restore(client, f"cp-serial-{i}").json()
        assert body["cursor_ms"] in (1000, 3000)
        assert len(body["channel"]["events"]) == body["channel"]["next_seq"] - 1


# ---------------------------------------------------------------------------
# 旧接口不变：存档/恢复叠加不改变既有响应
# ---------------------------------------------------------------------------


def test_legacy_responses_unchanged():
    client = make_client()
    sid = create_session(client, "scene-cp-legacy")
    client.post(f"/sessions/{sid}/channel")
    save_cp(client, sid, "cp-legacy")
    restore(client, "cp-legacy")
    # 通道开启响应、推进响应、补取响应字段均与之前一致
    open_body = client.post(
        "/sessions", json={"scene_id": "scene-cp-legacy"}
    )
    sid2 = open_body.json()["session_id"]
    assert set(open_body.json()) == {
        "session_id", "scene_id", "source_revision", "cursor_ms",
        "track_count", "changes", "subtitles", "assignments",
    }
    adv = events(client, sid2, 900)
    assert "events" in adv.json()
    plain = client.post(
        f"/sessions/{sid2}/advance", json={"time_ms": 950}
    ).json()
    assert set(plain) == {
        "session_id", "scene_id", "source_revision", "cursor_ms", "active"
    }
    client.post(f"/sessions/{sid2}/channel")
    fetch_body = fetch(client, sid2, 0).json()
    assert set(fetch_body) == {"session_id", "scene_id", "acked_seq", "events"}
