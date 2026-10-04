"""场景片段另存（裁剪）接口测试。

POST /scenes/{scene_id}/clip：把 [from_ms, to_ms) 另存为从 0 毫秒起、
保持源画面轨道的 revision=1 新场景。覆盖裁剪语义、保轨、边界、
404/409/422、原子性、源不变、既有接口可用，以及“源快照与核对版本
错配”的可控交错回归。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.solver import Subtitle, solve  # noqa: E402


def make_source(
    client,
    scene_id="scene-src",
    subtitles=None,
    track_count=2,
    expected_revision=0,
):
    if subtitles is None:
        # s1[0,1000) Alice；s2[800,3000) Bob 锁 2；s3[1000,2500) Alice
        # 分轨 s1=1, s2=2, s3=1，changes=0
        subtitles = [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ]
    resp = client.put(
        f"/scenes/{scene_id}",
        json={
            "track_count": track_count,
            "expected_revision": expected_revision,
            "subtitles": subtitles,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def clip(client, scene_id, target, expected_revision, from_ms, to_ms):
    return client.post(
        f"/scenes/{scene_id}/clip",
        json={
            "target_scene_id": target,
            "expected_revision": expected_revision,
            "from_ms": from_ms,
            "to_ms": to_ms,
        },
    )


# ---------------------------------------------------------------------------
# 基本裁剪语义
# ---------------------------------------------------------------------------


def test_clip_basic_shifts_times_and_preserves_tracks():
    client = TestClient(app)
    src = make_source(client)

    # 窗口 [900, 3000)：s1 尾部 [900,1000)、s2 整段 [900,3000)、
    # s3 整段 [1000,2500) 相交
    resp = clip(client, "scene-src", "scene-clip", 1, 900, 3000)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["scene_id"] == "scene-clip"
    assert body["revision"] == 1
    assert body["track_count"] == 2
    assert body["feasible"] is True

    assert body["subtitles"] == [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 100,
         "locked_track": 1},
        {"id": "s2", "speaker": "Bob", "start_ms": 0, "end_ms": 2100,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 100, "end_ms": 1600,
         "locked_track": 1},
    ]
    # 分轨与源画面轨道完全一致
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]

    # 片段已作为独立场景持久化
    got = client.get("/scenes/scene-clip").json()
    assert got["revision"] == 1
    assert got["subtitles"] == body["subtitles"]
    assert got["assignments"] == src["assignments"]


def test_clip_keeps_source_order_ids_and_speakers():
    client = TestClient(app)
    subtitles = [
        {"id": 9, "speaker": "Zoe", "start_ms": 0, "end_ms": 100},
        {"id": "x", "speaker": "Abe", "start_ms": 50, "end_ms": 200},
        {"id": 1, "speaker": "Zoe", "start_ms": 200, "end_ms": 300},
    ]
    make_source(client, "scene-order", subtitles)
    resp = clip(client, "scene-order", "scene-order-out", 1, 0, 1000)
    assert resp.status_code == 200
    subs = resp.json()["subtitles"]
    assert [s["id"] for s in subs] == [9, "x", 1]
    assert [s["speaker"] for s in subs] == ["Zoe", "Abe", "Zoe"]


def test_clip_boundary_intersection_is_half_open():
    client = TestClient(app)
    # s1[100,200) s2[200,300) s3[300,400)
    make_source(
        client,
        "scene-bound",
        subtitles=[
            {"id": "s1", "speaker": "A", "start_ms": 100, "end_ms": 200},
            {"id": "s2", "speaker": "B", "start_ms": 200, "end_ms": 300},
            {"id": "s3", "speaker": "C", "start_ms": 300, "end_ms": 400},
        ],
    )

    # 窗口 [200,300)：s1 end==from 不相交（右开），s3 start==to 不相交（左闭）
    resp = clip(client, "scene-bound", "scene-bound-out", 1, 200, 300)
    assert resp.status_code == 200
    subs = resp.json()["subtitles"]
    assert [s["id"] for s in subs] == ["s2"]
    assert subs[0]["start_ms"] == 0
    assert subs[0]["end_ms"] == 100
    assert subs[0]["locked_track"] == 1  # 单条源分轨为 1


def test_clip_partial_overlap_at_window_edges():
    client = TestClient(app)
    make_source(
        client,
        "scene-edges",
        subtitles=[
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 500},
            {"id": "b", "speaker": "B", "start_ms": 400, "end_ms": 600},
            {"id": "c", "speaker": "C", "start_ms": 1500, "end_ms": 2000},
        ],
    )
    # 窗口 [400,1500)：a 裁成 [400,500)->[0,100)，b [400,600)->[0,200)，
    # c start==to 不相交
    resp = clip(client, "scene-edges", "scene-edges-out", 1, 400, 1500)
    assert resp.status_code == 200
    subs = resp.json()["subtitles"]
    assert [(s["id"], s["start_ms"], s["end_ms"]) for s in subs] == [
        ("a", 0, 100),
        ("b", 0, 200),
    ]


def test_clip_starts_at_zero_and_recomputes_changes():
    """片段时间轴从 0 毫秒起；changes 按片段字幕按既有规则重算。"""
    client = TestClient(app)
    # 同一说话人被锁定到不同轨：源 changes=1（a->1, b->2）
    subtitles = [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100,
         "locked_track": 1},
        {"id": "b", "speaker": "A", "start_ms": 100, "end_ms": 200,
         "locked_track": 2},
    ]
    src = make_source(client, "scene-chg", subtitles)
    assert src["changes"] == 1

    # 只保留 b：片段内单条字幕，换轨 0
    resp = clip(client, "scene-chg", "scene-chg-out", 1, 100, 200)
    assert resp.status_code == 200
    body = resp.json()
    assert body["subtitles"] == [
        {"id": "b", "speaker": "A", "start_ms": 0, "end_ms": 100,
         "locked_track": 2}
    ]
    assert body["changes"] == 0
    assert body["assignments"] == [{"id": "b", "track": 2}]


def test_clip_locked_tracks_equal_source_assignments_even_if_unlocked():
    """源里未显式锁轨的字幕，片段 locked_track 写入源求解出的轨道。"""
    client = TestClient(app)
    subtitles = [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100},
        {"id": "b", "speaker": "B", "start_ms": 50, "end_ms": 150},
        {"id": "c", "speaker": "A", "start_ms": 100, "end_ms": 200},
    ]
    src = make_source(client, "scene-lock", subtitles)
    assert src["assignments"] == [
        {"id": "a", "track": 1},
        {"id": "b", "track": 2},
        {"id": "c", "track": 1},
    ]
    body = clip(client, "scene-lock", "scene-lock-out", 1, 0, 200).json()
    assert [s["locked_track"] for s in body["subtitles"]] == [1, 2, 1]


# ---------------------------------------------------------------------------
# 422：窗口无相交字幕 / 非法请求
# ---------------------------------------------------------------------------


def test_clip_without_intersection_returns_422_and_leaves_nothing():
    client = TestClient(app)
    make_source(client)
    # 所有字幕都在 [5000,6000) 之前结束
    resp = clip(client, "scene-src", "scene-empty", 1, 5000, 6000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["from_ms"] == 5000
    assert detail["to_ms"] == 6000

    # 失败不留下目标，源不变
    assert client.get("/scenes/scene-empty").status_code == 404
    src = client.get("/scenes/scene-src").json()
    assert src["revision"] == 1
    assert len(src["subtitles"]) == 3


def test_clip_gap_between_subtitles_returns_422():
    client = TestClient(app)
    make_source(
        client,
        "scene-gap",
        subtitles=[
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100},
            {"id": "b", "speaker": "B", "start_ms": 200, "end_ms": 300},
        ],
    )
    # [100,200) 两端都端点相接但左闭右开：无相交
    resp = clip(client, "scene-gap", "scene-gap-out", 1, 100, 200)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload_override",
    [
        {"from_ms": 100, "to_ms": 100},       # from == to
        {"from_ms": 200, "to_ms": 100},       # from > to
        {"from_ms": -1, "to_ms": 100},        # 负数
        {"from_ms": True, "to_ms": 100},      # 布尔值
        {"from_ms": 0, "to_ms": True},
        {"from_ms": False, "to_ms": 100},
        {"from_ms": "0", "to_ms": 100},       # 字符串
        {"from_ms": 0, "to_ms": "100"},
        {"from_ms": 1.0, "to_ms": 100},       # 浮点
        {"expected_revision": -1},            # 负版本号
        {"target_scene_id": "   "},           # 空白目标 id
        {"target_scene_id": ""},
    ],
)
def test_clip_invalid_request_returns_422(payload_override):
    client = TestClient(app)
    make_source(client)
    body = {
        "target_scene_id": "scene-bad",
        "expected_revision": 1,
        "from_ms": 0,
        "to_ms": 1000,
    }
    body.update(payload_override)
    resp = client.post("/scenes/scene-src/clip", json=body)
    assert resp.status_code == 422, resp.text
    assert client.get("/scenes/scene-bad").status_code == 404


def test_clip_missing_fields_422():
    client = TestClient(app)
    make_source(client)
    for body in (
        {"expected_revision": 1, "from_ms": 0, "to_ms": 100},
        {"target_scene_id": "t", "from_ms": 0, "to_ms": 100},
        {"target_scene_id": "t", "expected_revision": 1, "to_ms": 100},
        {"target_scene_id": "t", "expected_revision": 1, "from_ms": 0},
    ):
        resp = client.post("/scenes/scene-src/clip", json=body)
        assert resp.status_code == 422


def test_clip_blank_source_scene_id_422():
    client = TestClient(app)
    resp = client.post(
        "/scenes/%20%20/clip",
        json={"target_scene_id": "t", "expected_revision": 0,
              "from_ms": 0, "to_ms": 100},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 404 / 409
# ---------------------------------------------------------------------------


def test_clip_unknown_source_returns_404():
    client = TestClient(app)
    resp = clip(client, "no-such-scene", "scene-new", 0, 0, 100)
    assert resp.status_code == 404
    assert client.get("/scenes/scene-new").status_code == 404


def test_clip_stale_revision_returns_409():
    client = TestClient(app)
    make_source(client, "scene-rev")  # revision 1
    # 推进到 revision 2
    make_source(
        client,
        "scene-rev",
        expected_revision=1,
        subtitles=[
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 900},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000},
        ],
    )

    resp = clip(client, "scene-rev", "scene-rev-out", 1, 0, 1000)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "scene-rev"
    assert detail["current_revision"] == 2

    # 失败不留目标、源停留在 revision 2
    assert client.get("/scenes/scene-rev-out").status_code == 404
    assert client.get("/scenes/scene-rev").json()["revision"] == 2


def test_clip_expected_revision_zero_on_existing_source_409():
    client = TestClient(app)
    make_source(client)
    resp = clip(client, "scene-src", "scene-zero", 0, 0, 1000)
    assert resp.status_code == 409
    assert client.get("/scenes/scene-zero").status_code == 404


def test_clip_target_already_exists_returns_409():
    client = TestClient(app)
    make_source(client, "scene-a")
    make_source(client, "scene-b")  # 占用 scene-b

    resp = clip(client, "scene-a", "scene-b", 1, 0, 1000)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "scene-b"
    assert detail["current_revision"] == 1

    # 已存在目标保持原样（不会被片段覆盖）
    target = client.get("/scenes/scene-b").json()
    assert [s["id"] for s in target["subtitles"]] == ["s1", "s2", "s3"]
    # 源场景不受影响
    assert client.get("/scenes/scene-a").json()["revision"] == 1


def test_clip_target_same_as_source_returns_409():
    client = TestClient(app)
    make_source(client, "scene-same")
    resp = clip(client, "scene-same", "scene-same", 1, 0, 1000)
    assert resp.status_code == 409
    # 源没有被片段覆盖
    src = client.get("/scenes/scene-same").json()
    assert src["revision"] == 1
    assert len(src["subtitles"]) == 3


def test_clip_target_exists_conflict_check_after_empty_window_still_422():
    """窗口无相交（422 语义）优先于目标占用（409）：先裁得出片段才提交。"""
    client = TestClient(app)
    make_source(client, "scene-x")
    make_source(client, "scene-y")
    resp = clip(client, "scene-x", "scene-y", 1, 9000, 10000)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 原子性、源不变、独立性
# ---------------------------------------------------------------------------


def test_clip_does_not_modify_source_or_playback_sessions():
    client = TestClient(app)
    src_before = make_source(client, "scene-keep")
    sess = client.post(
        "/sessions", json={"scene_id": "scene-keep"}
    ).json()
    client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 900}
    )

    resp = clip(client, "scene-keep", "scene-keep-clip", 1, 900, 3000)
    assert resp.status_code == 200

    # 源场景快照逐字段不变
    assert client.get("/scenes/scene-keep").json() == src_before

    # 既有播放会话不被裁剪操作影响（游标仍在 900，快照仍为源）
    state = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 900}
    ).json()
    assert state["cursor_ms"] == 900
    assert state["source_revision"] == 1
    assert {a["id"] for a in client.get("/scenes/scene-keep").json()["assignments"]} == {
        "s1", "s2", "s3"
    }


def test_clip_is_independent_of_later_source_revision():
    client = TestClient(app)
    make_source(client, "scene-ind")
    resp = clip(client, "scene-ind", "scene-ind-out", 1, 0, 3000)
    assert resp.status_code == 200

    # 源继续修订到 revision 2；片段仍是 revision 1、内容不变
    make_source(
        client,
        "scene-ind",
        expected_revision=1,
        subtitles=[
            {"id": "only", "speaker": "Solo", "start_ms": 0, "end_ms": 50}
        ],
    )
    clip_got = client.get("/scenes/scene-ind-out").json()
    assert clip_got["revision"] == 1
    assert [s["id"] for s in clip_got["subtitles"]] == ["s1", "s2", "s3"]
    assert client.get("/scenes/scene-ind").json()["revision"] == 2


def test_clip_target_supports_existing_revise_read_and_playback():
    """片段目标沿用既有读取/修订/播放调用，旧接口不变。"""
    client = TestClient(app)
    make_source(client, "scene-play-src")
    resp = clip(client, "scene-play-src", "scene-play-clip", 1, 900, 3000)
    assert resp.status_code == 200

    # 修订片段：expected_revision=1，成功后 revision=2
    rev = client.put(
        "/scenes/scene-play-clip",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "s1", "speaker": "Alice", "start_ms": 0,
                 "end_ms": 100, "locked_track": 1},
                {"id": "s2", "speaker": "Bob", "start_ms": 0, "end_ms": 2100},
            ],
        },
    )
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2

    # 播放片段
    sess = client.post(
        "/sessions", json={"scene_id": "scene-play-clip"}
    ).json()
    assert sess["source_revision"] == 2
    assert sess["cursor_ms"] == 0
    adv = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 50}
    ).json()
    assert {a["id"] for a in adv["active"]} == {"s1", "s2"}

    # 事件推进同样可用（s1 在 100 退出）
    ev = client.post(
        f"/sessions/{sess['session_id']}/events", json={"time_ms": 100}
    ).json()
    assert ev["events"] == [
        {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 100,
         "type": "exit"}
    ]


def test_concurrent_clip_and_revision_never_mixes_snapshots():
    """并发裁剪与源修订：二者被数据库写锁串行，不出现混用/脏写。

    若裁剪先提交：片段 revision=1 基于 revision=1 的源，随后修订成功到
    revision=2；若修订先提交：裁剪收到 409。无论如何片段内容必须与某次
    完整提交一致，目标 revision 恒为 1。
    """
    client = TestClient(app)
    make_source(client, "scene-race")

    results = []
    barrier = threading.Barrier(2)

    def do_clip():
        local = TestClient(app)
        barrier.wait()
        resp = clip(local, "scene-race", "scene-race-clip", 1, 0, 3000)
        results.append(("clip", resp.status_code, resp.json()))

    def do_revise():
        local = TestClient(app)
        body = {
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "only", "speaker": "Solo",
                 "start_ms": 0, "end_ms": 50}
            ],
        }
        barrier.wait()
        resp = local.put("/scenes/scene-race", json=body)
        results.append(("revise", resp.status_code, resp.json()))

    threads = [threading.Thread(target=do_clip), threading.Thread(target=do_revise)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    clip_result = next(r for r in results if r[0] == "clip")
    revise_result = next(r for r in results if r[0] == "revise")
    assert revise_result[1] == 200
    assert revise_result[2]["revision"] == 2

    if clip_result[1] == 200:
        # 裁剪先生效：片段含 revision 1 的三条字幕
        body = clip_result[2]
        assert body["revision"] == 1
        assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3"]
    else:
        # 修订先生效：expected_revision=1 已过期
        assert clip_result[1] == 409
        assert clip_result[2]["detail"]["current_revision"] == 2


def test_concurrent_clips_to_same_target_only_one_succeeds():
    client = TestClient(app)
    make_source(client, "scene-multi")
    results = []
    barrier = threading.Barrier(4)

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = clip(local, "scene-multi", "scene-multi-out", 1, 0, 3000)
        results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 3
    assert client.get("/scenes/scene-multi-out").json()["revision"] == 1
    assert client.get("/scenes/scene-multi").json()["revision"] == 1


# ---------------------------------------------------------------------------
# 与求解器交叉验证
# ---------------------------------------------------------------------------


def test_clip_tracks_match_solver_with_locked_source_tracks():
    """片段 locked_track=源分轨时，求解器在源 track_count 下复现源轨道。"""
    client = TestClient(app)
    subtitles = [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000,
         "locked_track": 1},
        {"id": "b", "speaker": "B", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "c", "speaker": "A", "start_ms": 1000, "end_ms": 2500},
        {"id": "d", "speaker": "C", "start_ms": 2000, "end_ms": 2600},
    ]
    src = make_source(client, "scene-solver", subtitles, track_count=3)
    src_tracks = {a["id"]: a["track"] for a in src["assignments"]}

    resp = clip(client, "scene-solver", "scene-solver-out", 1, 900, 2700)
    assert resp.status_code == 200
    body = resp.json()

    subs = [
        Subtitle(
            id=s["id"], speaker=s["speaker"], start_ms=s["start_ms"],
            end_ms=s["end_ms"], locked_track=s["locked_track"],
        )
        for s in body["subtitles"]
    ]
    tracks, changes = solve(subs, 3)
    assert [a["track"] for a in body["assignments"]] == tracks
    assert body["changes"] == changes
    # 每条片段字幕的轨道都与源画面轨道相同
    for s in body["subtitles"]:
        assert s["locked_track"] == src_tracks[s["id"]]


# ---------------------------------------------------------------------------
# 回归：源快照与 expected_revision 错配（可控交错）
#
# 旧实现在读取源快照时不核对版本，随后由提交事务内的版本核对兜底。
# 若请求 expected_revision=2 先读到源版本 1、源再升级到 2，事务核对
# 恰好通过，版本 1 的字幕/分轨就会被当作“版本 2 的片段”保存。
# 修复后：读取时版本不符立即 409；读取匹配后、提交前源被修订仍由
# 事务内核对返回 409；两道核对之间源不变时正常裁剪。
# ---------------------------------------------------------------------------


def _v2_source_payload():
    """版本 2 的源内容：与 make_source 默认版本 1 明显不同。"""
    return {
        "track_count": 2,
        "expected_revision": 1,
        "subtitles": [
            {"id": "v2-a", "speaker": "Solo", "start_ms": 0, "end_ms": 5000},
        ],
    }


def _patch_clip_gates(monkeypatch, *, pause_at_read, pause_at_commit):
    """故障注入：在裁剪路由的两个边界点受控暂停。

    - pause_at_read：裁剪请求第一次 get_scene 读到源记录后置位，
      线程阻塞等待返回的 release_read；放行后返回该记录（模拟“读到
      后挂起”）；
    - pause_at_commit：裁剪请求调用 commit_clip 之前置位并等待
      release_commit（仅暂停第一次，避免影响主测试线程后续读取）。
    返回 (release_read, release_commit)。
    """
    release_read = threading.Event()
    release_commit = threading.Event()
    store = main_module.get_store()
    real_get_scene = store.get_scene
    real_commit_clip = store.commit_clip
    read_done = []

    def wrapped_get_scene(scene_id):
        if not read_done:
            read_done.append(scene_id)
            record = real_get_scene(scene_id)
            pause_at_read.set()
            assert release_read.wait(timeout=10), "测试未及时放行被暂停的读取"
            # 故意不重新读取：放行后仍交付手中的（可能已过时的）快照
            return record
        return real_get_scene(scene_id)

    commit_done = []

    def wrapped_commit_clip(*args, **kwargs):
        if not commit_done:
            commit_done.append(args)
            pause_at_commit.set()
            assert release_commit.wait(timeout=10), "测试未及时放行被暂停的提交"
        return real_commit_clip(*args, **kwargs)

    monkeypatch.setattr(store, "get_scene", wrapped_get_scene)
    monkeypatch.setattr(store, "commit_clip", wrapped_commit_clip)
    return release_read, release_commit


def _post_clip_in_thread(scene_id, target, expected_revision, responses, key):
    local = TestClient(app)
    responses[key] = clip(local, scene_id, target, expected_revision, 0, 3000)


def test_clip_reads_wrong_revision_then_source_catches_up_returns_409(
    monkeypatch,
):
    """expected_revision=2 先读到源版本 1；暂停期间源升级到 2。

    旧实现会放行并把版本 1 的字幕与分轨存为片段；修复后读取即核对，
    立即返回 409，且不创建目标、不改源场景、不动播放会话。
    """
    client = TestClient(app)
    src = make_source(client, "scene-mismatch")  # revision 1
    # 预先在源上建立播放会话，确认失败路径不影响会话
    sid = client.post(
        "/sessions", json={"scene_id": "scene-mismatch"}
    ).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    pause_at_read = threading.Event()
    pause_at_commit = threading.Event()  # 本用例不应到达提交点
    release_read, release_commit = _patch_clip_gates(
        monkeypatch,
        pause_at_read=pause_at_read,
        pause_at_commit=pause_at_commit,
    )
    # 旧实现会越过读取核对走到提交：预先放行提交门，让它干脆地以 200
    # 失败（而非挂在注入点）；修复后裁剪根本到不了提交门，放行无副作用。
    release_commit.set()

    responses = {}
    t = threading.Thread(
        target=_post_clip_in_thread,
        args=("scene-mismatch", "scene-mismatch-out", 2, responses, "clip"),
    )
    t.start()
    assert pause_at_read.wait(timeout=10), "裁剪线程未读到源版本 1"

    # 源在在途裁剪暂停期间从版本 1 升级到版本 2
    rev = client.put("/scenes/scene-mismatch", json=_v2_source_payload())
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2

    release_read.set()  # 放行裁剪：旧实现继续用版本 1 快照提交
    t.join(timeout=10)
    assert not t.is_alive()

    resp = responses["clip"]
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "scene-mismatch"
    assert detail["current_revision"] == 1  # 如实报告读到的版本

    # 失败不留目标；源停留在版本 2、内容为版本 2
    assert client.get("/scenes/scene-mismatch-out").status_code == 404
    source = client.get("/scenes/scene-mismatch").json()
    assert source["revision"] == 2
    assert [s["id"] for s in source["subtitles"]] == ["v2-a"]

    # 播放会话仍是版本 1 的内存快照（裁剪失败本就不应触碰会话）
    state = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 900}
    ).json()
    assert state["source_revision"] == 1
    assert state["cursor_ms"] == 900
    assert {a["id"] for a in state["active"]} == {"s1", "s2"}


def test_clip_source_revised_after_matched_read_returns_409(monkeypatch):
    """读取时版本匹配（1==1）；提交前源被修订到 2。

    提交事务内的第二道核对必须拦住：返回 409，不留目标、源停留在版本 2。
    """
    client = TestClient(app)
    make_source(client, "scene-bump")  # revision 1

    pause_at_read = threading.Event()
    pause_at_commit = threading.Event()
    release_read, release_commit = _patch_clip_gates(
        monkeypatch,
        pause_at_read=pause_at_read,
        pause_at_commit=pause_at_commit,
    )

    responses = {}
    t = threading.Thread(
        target=_post_clip_in_thread,
        args=("scene-bump", "scene-bump-out", 1, responses, "clip"),
    )
    t.start()
    assert pause_at_read.wait(timeout=10), "裁剪线程未完成读取"
    release_read.set()  # 读取核对通过，放行进入裁剪与提交前
    assert pause_at_commit.wait(timeout=10), "裁剪线程未到达提交点"

    # 提交前：源升级到版本 2
    rev = client.put("/scenes/scene-bump", json=_v2_source_payload())
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2

    release_commit.set()  # 放行提交：事务内核对应返回 409
    t.join(timeout=10)
    assert not t.is_alive()

    resp = responses["clip"]
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "scene-bump"
    assert detail["current_revision"] == 2

    assert client.get("/scenes/scene-bump-out").status_code == 404
    source = client.get("/scenes/scene-bump").json()
    assert source["revision"] == 2
    assert [s["id"] for s in source["subtitles"]] == ["v2-a"]


def test_clip_uninterrupted_flow_through_gates_succeeds(monkeypatch):
    """两道核对之间源版本不变：正常裁剪仍成功，行为与原语义一致。

    时间按源顺序归零、保持源画面轨道、保存为版本 1，目标可读取、
    修订与播放；源场景与其播放会话不变。
    """
    client = TestClient(app)
    src_before = make_source(client, "scene-ok")  # revision 1
    sid = client.post(
        "/sessions", json={"scene_id": "scene-ok"}
    ).json()["session_id"]

    pause_at_read = threading.Event()
    pause_at_commit = threading.Event()
    release_read, release_commit = _patch_clip_gates(
        monkeypatch,
        pause_at_read=pause_at_read,
        pause_at_commit=pause_at_commit,
    )

    responses = {}
    t = threading.Thread(
        target=_post_clip_in_thread,
        args=("scene-ok", "scene-ok-out", 1, responses, "clip"),
    )
    t.start()
    assert pause_at_read.wait(timeout=10)
    release_read.set()
    assert pause_at_commit.wait(timeout=10)
    # 暂停期间源保持版本 1 不变，直接放行提交
    release_commit.set()
    t.join(timeout=10)
    assert not t.is_alive()

    resp = responses["clip"]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scene_id"] == "scene-ok-out"
    assert body["revision"] == 1
    # 时间归零、源顺序、源画面轨道保持
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3"]
    assert body["assignments"] == src_before["assignments"]

    # 目标可读取
    got = client.get("/scenes/scene-ok-out").json()
    assert got == body

    # 目标可修订（revision 1 -> 2）
    rev = client.put(
        "/scenes/scene-ok-out",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "s1", "speaker": "Alice", "start_ms": 0,
                 "end_ms": 100, "locked_track": 1},
            ],
        },
    )
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2

    # 目标可播放
    csess = client.post(
        "/sessions", json={"scene_id": "scene-ok-out"}
    ).json()
    assert csess["source_revision"] == 2

    # 源场景与其播放会话不受影响
    assert client.get("/scenes/scene-ok").json() == src_before
    state = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 0}
    ).json()
    assert state["source_revision"] == 1
    assert state["cursor_ms"] == 0
