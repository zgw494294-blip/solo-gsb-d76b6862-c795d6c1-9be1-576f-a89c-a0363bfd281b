"""已保存场景拼接（concat）接口测试。

POST /scenes/concat：按给定顺序把 2～4 个已保存场景拼成连续播放
场景。覆盖时间平移与 id/speaker/保轨语义、changes 跨段计算、段序
排列、同源重复、严格整数、2～4 段约束、轨道数一致/时长/字幕总数
422、404/409、原子性、源与播放会话不变、成功后可读/改/播，以及
“读取后、提交前源被修订”的可控交错回归。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.solver import Subtitle, solve  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def make_scene(client, scene_id, subtitles, track_count=2, expected_revision=0):
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


def concat(client, target, segments):
    return client.post(
        "/scenes/concat",
        json={"target_scene_id": target, "segments": segments},
    )


def seg(scene_id, expected_revision=1, duration_ms=None):
    if duration_ms is None:
        duration_ms = 10_000
    return {
        "scene_id": scene_id,
        "expected_revision": expected_revision,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# 基本拼接语义
# ---------------------------------------------------------------------------


def test_concat_two_sources_shifts_times_and_rewrites_ids():
    client = TestClient(app)
    make_scene(
        client,
        "sc-a",
        [
            {"id": "a1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "a2", "speaker": "Bob", "start_ms": 800, "end_ms": 2000,
             "locked_track": 2},
        ],
    )  # 分轨 a1=1, a2=2，max_end=2000
    make_scene(
        client,
        "sc-b",
        [
            {"id": "b1", "speaker": "Alice", "start_ms": 0, "end_ms": 1500},
        ],
    )  # 分轨 b1=1，max_end=1500

    resp = concat(
        client,
        "sc-out",
        [seg("sc-a", duration_ms=2000), seg("sc-b", duration_ms=1500)],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["scene_id"] == "sc-out"
    assert body["revision"] == 1
    assert body["track_count"] == 2
    assert body["feasible"] is True

    assert body["subtitles"] == [
        {"id": "1:1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000,
         "locked_track": 1},
        {"id": "1:2", "speaker": "Bob", "start_ms": 800, "end_ms": 2000,
         "locked_track": 2},
        {"id": "2:1", "speaker": "Alice", "start_ms": 2000, "end_ms": 3500,
         "locked_track": 1},
    ]
    assert body["assignments"] == [
        {"id": "1:1", "track": 1},
        {"id": "1:2", "track": 2},
        {"id": "2:1", "track": 1},
    ]
    # Alice 的 a1->b1 跨段时间相邻且同轨；Bob 只有一条：changes=0
    assert body["changes"] == 0

    # 已作为独立场景持久化
    assert client.get("/scenes/sc-out").json() == body


def test_concat_segment_order_is_request_order():
    """同样的两段，段序调换后时间轴顺序与 id 段号随之调换。"""
    client = TestClient(app)
    make_scene(
        client, "sc-p",
        [{"id": "p", "speaker": "P", "start_ms": 0, "end_ms": 100}],
    )
    make_scene(
        client, "sc-q",
        [{"id": "q", "speaker": "Q", "start_ms": 0, "end_ms": 100}],
    )
    body = concat(
        client, "sc-order",
        [seg("sc-q", duration_ms=100), seg("sc-p", duration_ms=100)],
    ).json()
    assert [(s["id"], s["speaker"], s["start_ms"]) for s in body["subtitles"]] == [
        ("1:1", "Q", 0),
        ("2:1", "P", 100),
    ]


def test_concat_preserves_within_source_order():
    client = TestClient(app)
    make_scene(
        client, "sc-ord",
        [
            {"id": 9, "speaker": "Zoe", "start_ms": 0, "end_ms": 50},
            {"id": "x", "speaker": "Abe", "start_ms": 50, "end_ms": 100},
            {"id": 1, "speaker": "Zoe", "start_ms": 100, "end_ms": 150},
        ],
    )
    make_scene(
        client, "sc-ord2",
        [{"id": "z", "speaker": "Abe", "start_ms": 0, "end_ms": 50}],
    )
    body = concat(
        client, "sc-ord-out",
        [seg("sc-ord", duration_ms=150), seg("sc-ord2", duration_ms=50)],
    ).json()
    assert [(s["id"], s["speaker"]) for s in body["subtitles"]] == [
        ("1:1", "Zoe"), ("1:2", "Abe"), ("1:3", "Zoe"), ("2:1", "Abe")
    ]


def test_concat_shifts_by_cumulative_durations_with_three_segments():
    client = TestClient(app)
    make_scene(client, "s1", [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 10}])
    make_scene(client, "s2", [
        {"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 10}])
    make_scene(client, "s3", [
        {"id": "c", "speaker": "C", "start_ms": 0, "end_ms": 10}])

    body = concat(
        client, "s-out",
        [seg("s1", duration_ms=100),
         seg("s2", duration_ms=200),
         seg("s3", duration_ms=300)],
    ).json()
    assert [(s["id"], s["start_ms"], s["end_ms"]) for s in body["subtitles"]] == [
        ("1:1", 0, 10),
        ("2:1", 100, 110),
        ("3:1", 300, 310),
    ]


def test_concat_allows_same_source_repeated():
    """同源可重复：同一场景可作为多段，版本逐段核对。"""
    client = TestClient(app)
    make_scene(
        client, "sc-rep",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100}],
    )
    resp = concat(
        client, "sc-rep-out",
        [seg("sc-rep", duration_ms=100), seg("sc-rep", duration_ms=100)],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [(s["id"], s["start_ms"], s["end_ms"], s["locked_track"])
            for s in body["subtitles"]] == [
        ("1:1", 0, 100, 1),
        ("2:1", 100, 200, 1),
    ]
    # 同一说话人跨段时间相邻且同轨
    assert body["changes"] == 0


def test_concat_four_segments_allowed():
    client = TestClient(app)
    for i in range(1, 5):
        make_scene(
            client, f"sc4-{i}",
            [{"id": "x", "speaker": f"S{i}", "start_ms": 0, "end_ms": 1}],
            track_count=3,
        )
    resp = concat(
        client, "sc4-out",
        [seg(f"sc4-{i}", duration_ms=1) for i in range(1, 5)],
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["subtitles"]) == 4
    assert resp.json()["track_count"] == 3


def test_concat_id_format_always_colon_strings():
    """新 id 恒为“段号:段内序号”字符串，即使源 id 是整数。"""
    client = TestClient(app)
    make_scene(client, "sc-i", [
        {"id": 1, "speaker": "A", "start_ms": 0, "end_ms": 1}])
    make_scene(client, "sc-i2", [
        {"id": 2, "speaker": "B", "start_ms": 0, "end_ms": 1}])
    body = concat(
        client, "sc-i-out",
        [seg("sc-i", duration_ms=1), seg("sc-i2", duration_ms=1)],
    ).json()
    assert [s["id"] for s in body["subtitles"]] == ["1:1", "2:1"]
    assert all(isinstance(s["id"], str) for s in body["subtitles"])


def test_concat_locked_track_is_source_assignment_even_if_unlocked():
    """源里未显式锁轨求出的实际轨道也写入 locked_track。"""
    client = TestClient(app)
    make_scene(
        client, "sc-lock",
        [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100},
            {"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 100},
            {"id": "c", "speaker": "A", "start_ms": 100, "end_ms": 200},
        ],
    )  # 分轨 a=1, b=2, c=1
    make_scene(
        client, "sc-lock2",
        [{"id": "d", "speaker": "A", "start_ms": 0, "end_ms": 100}],
    )
    body = concat(
        client, "sc-lock-out",
        [seg("sc-lock", duration_ms=200), seg("sc-lock2", duration_ms=100)],
    ).json()
    assert [s["locked_track"] for s in body["subtitles"]] == [1, 2, 1, 1]
    assert [a["track"] for a in body["assignments"]] == [1, 2, 1, 1]


def test_concat_changes_counts_cross_segment_speaker_adjacency():
    """changes 按拼接后同说话人时间相邻字幕计算，跨段相邻计入换轨。"""
    client = TestClient(app)
    # 段 1：Alice 锁 1；段 2：与 Bob 重叠迫使 Alice 到 2 轨
    make_scene(
        client, "sc-c1",
        [{"id": "a", "speaker": "Alice", "start_ms": 0, "end_ms": 100,
          "locked_track": 1}],
    )
    make_scene(
        client, "sc-c2",
        [
            {"id": "b", "speaker": "Bob", "start_ms": 0, "end_ms": 100,
             "locked_track": 1},
            {"id": "a2", "speaker": "Alice", "start_ms": 0, "end_ms": 100},
        ],
    )  # Alice 在源 2 中落 2 轨
    body = concat(
        client, "sc-c-out",
        [seg("sc-c1", duration_ms=100), seg("sc-c2", duration_ms=100)],
    ).json()
    tracks = {a["id"]: a["track"] for a in body["assignments"]}
    assert tracks == {"1:1": 1, "2:1": 1, "2:2": 2}
    # Alice 的 1:1(轨1) 与 2:2(轨2) 跨段时间相邻，换轨 1
    assert body["changes"] == 1


def test_concat_no_change_when_speaker_resumes_same_track():
    client = TestClient(app)
    make_scene(
        client, "sc-n1",
        [{"id": "a", "speaker": "Alice", "start_ms": 0, "end_ms": 100}],
    )
    make_scene(
        client, "sc-n2",
        [
            {"id": "x", "speaker": "Bob", "start_ms": 0, "end_ms": 100},
            {"id": "a2", "speaker": "Alice", "start_ms": 0, "end_ms": 100},
        ],
    )  # Alice 与 Bob 重叠：Bob 字典序取 1，Alice 取 2？验证实际分轨
    src2 = client.get("/scenes/sc-n2").json()
    alice_track = next(
        a["track"] for a in src2["assignments"]
        if a["id"] == "a2"
    )
    # 段 1 单条 Alice 求解为 1；若段 2 Alice 在 2 则 changes=1，同轨则 0
    body = concat(
        client, "sc-n-out",
        [seg("sc-n1", duration_ms=100), seg("sc-n2", duration_ms=100)],
    ).json()
    track_1 = next(a["track"] for a in body["assignments"] if a["id"] == "1:1")
    track_2 = next(a["track"] for a in body["assignments"] if a["id"] == "2:2")
    assert track_1 == 1
    assert track_2 == alice_track
    assert body["changes"] == (0 if track_1 == track_2 else 1)


def test_concat_matches_solver_on_built_subtitles():
    """拼接结果与对平移后字幕（locked_track=源分轨）直接求解一致。"""
    client = TestClient(app)
    make_scene(
        client, "sc-v1",
        [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000,
             "locked_track": 1},
            {"id": "b", "speaker": "B", "start_ms": 800, "end_ms": 3000},
            {"id": "c", "speaker": "A", "start_ms": 1000, "end_ms": 2000},
        ],
        track_count=3,
    )
    make_scene(
        client, "sc-v2",
        [
            {"id": "d", "speaker": "B", "start_ms": 0, "end_ms": 500},
            {"id": "e", "speaker": "A", "start_ms": 500, "end_ms": 1500},
        ],
        track_count=3,
    )
    body = concat(
        client, "sc-v-out",
        [seg("sc-v1", duration_ms=3000), seg("sc-v2", duration_ms=1500)],
    ).json()
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


def test_concat_duration_equal_to_max_end_is_allowed():
    client = TestClient(app)
    make_scene(
        client, "sc-eq",
        [{"id": "a", "speaker": "A", "start_ms": 90, "end_ms": 100}],
    )
    resp = concat(
        client, "sc-eq-out",
        [seg("sc-eq", duration_ms=100), seg("sc-eq", duration_ms=100)],
    )
    assert resp.status_code == 200, resp.text
    # 两条端点相接（end==start），同一说话人可同轨
    assert resp.json()["changes"] == 0


# ---------------------------------------------------------------------------
# 422：跨源业务约束
# ---------------------------------------------------------------------------


def test_concat_duration_shorter_than_max_end_returns_422():
    client = TestClient(app)
    make_scene(
        client, "sc-d",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 500}],
    )
    make_scene(
        client, "sc-d2",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 300}],
    )
    resp = concat(
        client, "sc-d-bad",
        [seg("sc-d", duration_ms=500), seg("sc-d2", duration_ms=299)],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["segment"] == 2
    assert detail["duration_ms"] == 299
    assert detail["max_end_ms"] == 300
    assert client.get("/scenes/sc-d-bad").status_code == 404
    # 源不变
    assert client.get("/scenes/sc-d2").json()["revision"] == 1


def test_concat_different_track_counts_returns_422():
    client = TestClient(app)
    make_scene(
        client, "sc-t2",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
        track_count=2,
    )
    make_scene(
        client, "sc-t3",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 1}],
        track_count=3,
    )
    resp = concat(
        client, "sc-t-bad",
        [seg("sc-t2", duration_ms=1), seg("sc-t3", duration_ms=1)],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["segment"] == 2
    assert detail["track_count"] == 3
    assert detail["expected_track_count"] == 2
    assert client.get("/scenes/sc-t-bad").status_code == 404


def test_concat_more_than_twelve_subtitles_returns_422():
    client = TestClient(app)
    # 源 1：7 条互不重叠同说话人字幕
    make_scene(
        client, "sc-big1",
        [
            {"id": f"a{i}", "speaker": "A",
             "start_ms": i * 10, "end_ms": i * 10 + 5}
            for i in range(7)
        ],
    )
    # 源 2：6 条
    make_scene(
        client, "sc-big2",
        [
            {"id": f"b{i}", "speaker": "B",
             "start_ms": i * 10, "end_ms": i * 10 + 5}
            for i in range(6)
        ],
    )
    resp = concat(
        client, "sc-big-out",
        [seg("sc-big1", duration_ms=100), seg("sc-big2", duration_ms=100)],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["total_subtitles"] == 13
    assert client.get("/scenes/sc-big-out").status_code == 404

    # 恰好 12 条：成功
    make_scene(
        client, "sc-big0",
        [
            {"id": f"c{i}", "speaker": "C",
             "start_ms": i * 10, "end_ms": i * 10 + 5}
            for i in range(6)
        ],
    )
    ok = concat(
        client, "sc-big-ok",
        [seg("sc-big0", duration_ms=100), seg("sc-big2", duration_ms=100)],
    )
    assert ok.status_code == 200
    assert len(ok.json()["subtitles"]) == 12


# ---------------------------------------------------------------------------
# 422：Pydantic 请求结构与严格整数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "segments_override",
    [
        [seg("sc-x")],  # 只有 1 段
        [seg("sc-x") for _ in range(5)],  # 5 段
        [],  # 空
    ],
)
def test_concat_segment_count_must_be_2_to_4(segments_override):
    client = TestClient(app)
    resp = concat(client, "sc-n", segments_override)
    assert resp.status_code == 422, resp.text
    assert client.get("/scenes/sc-n").status_code == 404


def test_concat_missing_segments_field_422():
    client = TestClient(app)
    resp = client.post("/scenes/concat", json={"target_scene_id": "t"})
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "segment_override",
    [
        {"expected_revision": 1, "duration_ms": 100},  # 缺 scene_id
        {"scene_id": "sc-x", "duration_ms": 100},  # 缺 expected_revision
        {"scene_id": "sc-x", "expected_revision": 1},  # 缺 duration_ms
        {"scene_id": "", "expected_revision": 1, "duration_ms": 100},
        {"scene_id": "   ", "expected_revision": 1, "duration_ms": 100},
        {"scene_id": "sc-x", "expected_revision": 0, "duration_ms": 100},
        {"scene_id": "sc-x", "expected_revision": -1, "duration_ms": 100},
        {"scene_id": "sc-x", "expected_revision": 1, "duration_ms": 0},
        {"scene_id": "sc-x", "expected_revision": 1, "duration_ms": -5},
        # 严格整数：布尔与字符串一律拒绝
        {"scene_id": "sc-x", "expected_revision": True,
         "duration_ms": 100},
        {"scene_id": "sc-x", "expected_revision": "1",
         "duration_ms": 100},
        {"scene_id": "sc-x", "expected_revision": 1,
         "duration_ms": True},
        {"scene_id": "sc-x", "expected_revision": 1,
         "duration_ms": "100"},
        {"scene_id": "sc-x", "expected_revision": 1,
         "duration_ms": 1.0},
        {"scene_id": 1, "expected_revision": 1, "duration_ms": 100},
    ],
)
def test_concat_invalid_segment_fields_422(segment_override):
    client = TestClient(app)
    make_scene(
        client, "sc-x",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    # 合法首段 + 非法段
    resp = concat(
        client, "sc-invalid",
        [seg("sc-x", duration_ms=1), segment_override],
    )
    assert resp.status_code == 422, resp.text
    assert client.get("/scenes/sc-invalid").status_code == 404


def test_concat_blank_target_scene_id_422():
    client = TestClient(app)
    make_scene(
        client, "sc-b",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    for target in ("", "   "):
        resp = concat(
            client, target,
            [seg("sc-b", duration_ms=1), seg("sc-b", duration_ms=1)],
        )
        assert resp.status_code == 422


def test_concat_segments_must_be_array_422():
    client = TestClient(app)
    resp = client.post(
        "/scenes/concat",
        json={"target_scene_id": "t", "segments": {"scene_id": "x"}},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 404 / 409
# ---------------------------------------------------------------------------


def test_concat_unknown_source_returns_404():
    client = TestClient(app)
    make_scene(
        client, "sc-ok",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    resp = concat(
        client, "sc-404",
        [seg("sc-ok", duration_ms=1), seg("nope", duration_ms=1)],
    )
    assert resp.status_code == 404
    assert client.get("/scenes/sc-404").status_code == 404

    # 第一段就不存在
    resp2 = concat(
        client, "sc-404b",
        [seg("nope", duration_ms=1), seg("sc-ok", duration_ms=1)],
    )
    assert resp2.status_code == 404
    assert client.get("/scenes/sc-404b").status_code == 404


def test_concat_stale_revision_returns_409_with_segment_index():
    client = TestClient(app)
    make_scene(
        client, "sc-r",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    # 修订到 revision 2
    make_scene(
        client, "sc-r", expected_revision=1,
        subtitles=[{"id": "a2", "speaker": "A", "start_ms": 0, "end_ms": 2}],
    )
    make_scene(
        client, "sc-r2",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 1}],
    )
    resp = concat(
        client, "sc-r-out",
        [
            seg("sc-r2", duration_ms=1),
            seg("sc-r", expected_revision=1, duration_ms=2),
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "sc-r"
    assert detail["segment"] == 2
    assert detail["current_revision"] == 2
    assert client.get("/scenes/sc-r-out").status_code == 404


def test_concat_repeated_source_with_wrong_revision_in_second_occurrence_409():
    client = TestClient(app)
    make_scene(
        client, "sc-rep",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    resp = concat(
        client, "sc-rep-bad",
        [
            seg("sc-rep", expected_revision=1, duration_ms=1),
            seg("sc-rep", expected_revision=2, duration_ms=1),
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["segment"] == 2
    assert detail["current_revision"] == 1
    assert client.get("/scenes/sc-rep-bad").status_code == 404


def test_concat_target_already_exists_returns_409():
    client = TestClient(app)
    make_scene(
        client, "sc-g",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    make_scene(
        client, "sc-taken",
        [{"id": "z", "speaker": "Z", "start_ms": 0, "end_ms": 1}],
    )
    resp = concat(
        client, "sc-taken",
        [seg("sc-g", duration_ms=1), seg("sc-g", duration_ms=1)],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "sc-taken"
    assert detail["current_revision"] == 1

    # 已存在目标不被覆盖；源不变
    target = client.get("/scenes/sc-taken").json()
    assert [s["id"] for s in target["subtitles"]] == ["z"]
    assert client.get("/scenes/sc-g").json()["revision"] == 1


def test_concat_target_same_as_source_returns_409():
    client = TestClient(app)
    make_scene(
        client, "sc-self",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    resp = concat(
        client, "sc-self",
        [seg("sc-self", duration_ms=1), seg("sc-self", duration_ms=1)],
    )
    assert resp.status_code == 409
    src = client.get("/scenes/sc-self").json()
    assert src["revision"] == 1
    assert [s["id"] for s in src["subtitles"]] == ["a"]


def test_concat_illegal_business_input_422_takes_precedence_over_target_409():
    """跨源约束非法（422）优先于目标占用（409）：提交前先在路由核对。"""
    client = TestClient(app)
    make_scene(
        client, "sc-p1",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 10}],
        track_count=2,
    )
    make_scene(
        client, "sc-p2",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 10}],
        track_count=3,
    )
    make_scene(
        client, "sc-occ",
        [{"id": "o", "speaker": "O", "start_ms": 0, "end_ms": 1}],
    )
    resp = concat(
        client, "sc-occ",
        [seg("sc-p1", duration_ms=10), seg("sc-p2", duration_ms=10)],
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 原子性 / 源与播放会话不变 / 目标可读取修订播放
# ---------------------------------------------------------------------------


def test_concat_does_not_modify_sources_or_playback_sessions():
    client = TestClient(app)
    src1_before = make_scene(
        client, "sc-keep1",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100}],
    )
    src2_before = make_scene(
        client, "sc-keep2",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 100}],
    )
    sess = client.post(
        "/sessions", json={"scene_id": "sc-keep1"}
    ).json()
    client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 50}
    )

    resp = concat(
        client, "sc-keep-out",
        [seg("sc-keep1", duration_ms=100), seg("sc-keep2", duration_ms=100)],
    )
    assert resp.status_code == 200

    assert client.get("/scenes/sc-keep1").json() == src1_before
    assert client.get("/scenes/sc-keep2").json() == src2_before

    # 播放会话仍是源 revision 1、游标 50
    state = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 50}
    ).json()
    assert state["cursor_ms"] == 50
    assert state["source_revision"] == 1


def test_concat_target_supports_read_revise_and_playback():
    client = TestClient(app)
    make_scene(
        client, "sc-use1",
        [{"id": "a", "speaker": "Alice", "start_ms": 0, "end_ms": 100}],
    )
    make_scene(
        client, "sc-use2",
        [{"id": "b", "speaker": "Bob", "start_ms": 0, "end_ms": 200}],
    )
    created = concat(
        client, "sc-use-out",
        [seg("sc-use1", duration_ms=100), seg("sc-use2", duration_ms=200)],
    ).json()
    assert created["revision"] == 1

    # 读取
    assert client.get("/scenes/sc-use-out").json() == created

    # 修订：revision 1 -> 2
    rev = client.put(
        "/scenes/sc-use-out",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "only", "speaker": "Solo",
                 "start_ms": 0, "end_ms": 50},
            ],
        },
    )
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2

    # 播放
    sess = client.post(
        "/sessions", json={"scene_id": "sc-use-out"}
    ).json()
    assert sess["source_revision"] == 2
    ev = client.post(
        f"/sessions/{sess['session_id']}/events", json={"time_ms": 50}
    ).json()
    assert ev["events"] == [
        {"id": "only", "speaker": "Solo", "track": 1,
         "time_ms": 50, "type": "exit"}
    ]

    # 源不受目标修订影响
    assert client.get("/scenes/sc-use1").json()["revision"] == 1


def test_concat_target_independent_of_later_source_revision():
    client = TestClient(app)
    make_scene(
        client, "sc-ind",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    make_scene(
        client, "sc-ind2",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 1}],
    )
    out = concat(
        client, "sc-ind-out",
        [seg("sc-ind", duration_ms=1), seg("sc-ind2", duration_ms=1)],
    ).json()

    # 源继续修订；拼接目标保持 revision 1、内容不变
    make_scene(
        client, "sc-ind", expected_revision=1,
        subtitles=[{"id": "new", "speaker": "A", "start_ms": 0, "end_ms": 9}],
    )
    got = client.get("/scenes/sc-ind-out").json()
    assert got == out
    assert got["revision"] == 1
    assert [s["id"] for s in got["subtitles"]] == ["1:1", "2:1"]


def test_concurrent_concats_to_same_target_only_one_succeeds():
    client = TestClient(app)
    make_scene(
        client, "sc-multi",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    results = []
    barrier = threading.Barrier(4)

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = concat(
            local,
            "sc-multi-out",
            [seg("sc-multi", duration_ms=1), seg("sc-multi", duration_ms=1)],
        )
        results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 3
    assert client.get("/scenes/sc-multi-out").json()["revision"] == 1
    assert client.get("/scenes/sc-multi").json()["revision"] == 1


# ---------------------------------------------------------------------------
# 提交前源被修订：两道版本核对（可控交错，回归）
# ---------------------------------------------------------------------------


def _patch_concat_gates(monkeypatch, *, pause_at_reads, pause_at_commit):
    """在拼接路由读取各源后、提交前注入暂停点。

    pause_at_reads：全部段的 get_scene 完成后只触发一次；
    pause_at_commit：commit_concat 调用前只触发一次。
    """
    release_reads = threading.Event()
    release_commit = threading.Event()
    store = main_module.get_store()
    real_get_scene = store.get_scene
    real_commit_concat = store.commit_concat
    state = {"reads_gate_done": False, "commit_done": False, "reads": 0}

    def wrapped_get_scene(scene_id):
        record = real_get_scene(scene_id)
        return record

    # 通过包装路由内 store.get_scene 无法得知“全部读取完成”，
    # 改为计数：测试只有两个段，第 2 次读取后挂门。
    def counting_get_scene(scene_id):
        record = real_get_scene(scene_id)
        state["reads"] += 1
        if state["reads"] == 2 and not state["reads_gate_done"]:
            state["reads_gate_done"] = True
            pause_at_reads.set()
            assert release_reads.wait(timeout=10), "测试未及时放行读取门"
        return record

    def wrapped_commit_concat(*args, **kwargs):
        if not state["commit_done"]:
            state["commit_done"] = True
            pause_at_commit.set()
            assert release_commit.wait(timeout=10), "测试未及时放行提交门"
        return real_commit_concat(*args, **kwargs)

    monkeypatch.setattr(store, "get_scene", counting_get_scene)
    monkeypatch.setattr(store, "commit_concat", wrapped_commit_concat)
    return release_reads, release_commit


def _post_concat_in_thread(target, segments, responses, key):
    local = TestClient(app)
    responses[key] = concat(local, target, segments)


def test_concat_source_revised_after_matched_reads_returns_409(monkeypatch):
    """读取时两段版本都匹配；提交前其中一个源被修订 -> 事务核对 409。"""
    client = TestClient(app)
    make_scene(
        client, "sc-bump1",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    make_scene(
        client, "sc-bump2",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 1}],
    )

    pause_at_reads = threading.Event()
    pause_at_commit = threading.Event()
    release_reads, release_commit = _patch_concat_gates(
        monkeypatch,
        pause_at_reads=pause_at_reads,
        pause_at_commit=pause_at_commit,
    )

    responses = {}
    t = threading.Thread(
        target=_post_concat_in_thread,
        args=(
            "sc-bump-out",
            [seg("sc-bump1", duration_ms=1), seg("sc-bump2", duration_ms=1)],
            responses, "concat",
        ),
    )
    t.start()
    assert pause_at_reads.wait(timeout=10), "拼接线程未完成两段读取"
    release_reads.set()
    assert pause_at_commit.wait(timeout=10), "拼接线程未到达提交点"

    # 提交前：源 1 升级到 revision 2
    rev = client.put(
        "/scenes/sc-bump1",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [{"id": "only", "speaker": "Solo",
                           "start_ms": 0, "end_ms": 1}],
        },
    )
    assert rev.status_code == 200

    release_commit.set()
    t.join(timeout=10)
    assert not t.is_alive()

    resp = responses["concat"]
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "sc-bump1"
    assert detail["current_revision"] == 2

    assert client.get("/scenes/sc-bump-out").status_code == 404
    assert client.get("/scenes/sc-bump1").json()["revision"] == 2
    assert client.get("/scenes/sc-bump2").json()["revision"] == 1


def test_concat_read_revision_mismatch_returns_409_before_commit(monkeypatch):
    """读取时版本不符立即 409，不会走到提交点，也不留目标。"""
    client = TestClient(app)
    make_scene(
        client, "sc-m1",
        [{"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1}],
    )
    make_scene(
        client, "sc-m2",
        [{"id": "b", "speaker": "B", "start_ms": 0, "end_ms": 1}],
    )

    pause_at_reads = threading.Event()
    pause_at_commit = threading.Event()
    release_reads, release_commit = _patch_concat_gates(
        monkeypatch,
        pause_at_reads=pause_at_reads,
        pause_at_commit=pause_at_commit,
    )
    # 读取核对就应失败：两个门都预先放行，保证即使越界也不挂死
    release_reads.set()
    release_commit.set()

    resp = concat(
        client, "sc-m-out",
        [seg("sc-m1", duration_ms=1),
         seg("sc-m2", expected_revision=9, duration_ms=1)],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["segment"] == 2
    assert detail["current_revision"] == 1
    assert client.get("/scenes/sc-m-out").status_code == 404
    assert not pause_at_commit.is_set()
