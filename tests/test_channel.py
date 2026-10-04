"""补取通道：推进响应丢失后补取未确认的字幕变更。

覆盖：
- 开启：返回当前可见 active 与序号 0；重复开启 409 且已有通道不变；
  未知或已结束会话 404；开启之前的事件不补记；
- 记录：普通推进、事件推进、隐藏切换产生的全部 enter/exit 都入队，
  短字幕不漏记，seq 从 1 连续编号，沿用事件字段与排序；隐藏说话人的
  字幕不入队；接纳不重写已排队内容；
- 补取：ack_seq 为严格非负整数（布尔/字符串/浮点/缺失/null 均 422）；
  只能确认此前补取已返回的最大 seq 以内的前缀，越界 422 且通道不变；
  旧确认不回退；返回确认位置与剩余事件（按 seq 递增）；未确认事件
  重复补取时内容与序号不变；
- 结束会话清除通道；未开启通道的补取 404；旧接口响应不变；
  失败不改会话或通道；开启/补取与既有会话操作完整串行。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


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


def make_session(client, scene_id="scene-chan"):
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()


def open_channel(client, sid):
    return client.post(f"/sessions/{sid}/channel")


def fetch(client, sid, ack_seq):
    return client.post(
        f"/sessions/{sid}/channel/fetch", json={"ack_seq": ack_seq}
    )


def _quads(body):
    return [
        (e["seq"], e["time_ms"], e["type"], e["id"]) for e in body["events"]
    ]


# ---------------------------------------------------------------------------
# 开启：当前可见 active 与序号 0；重复开启 409；404
# ---------------------------------------------------------------------------


def test_open_returns_visible_active_and_seq_zero():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    resp = open_channel(client, sid)
    assert resp.status_code == 200
    body = resp.json()
    assert body["seq"] == 0
    assert body["cursor_ms"] == 0
    assert body["source_revision"] == 1
    # 游标 0：仅 s1 [0,1000) 活动（s2 800 才开始）
    assert [(a["id"], a["track"]) for a in body["active"]] == [("s1", 1)]


def test_open_reflects_current_cursor_and_hidden():
    """开启时的 active 取当前游标的可见集合（排除隐藏说话人）。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )
    body = open_channel(client, sid).json()
    assert body["cursor_ms"] == 900
    assert [a["id"] for a in body["active"]] == ["s2"]


def test_reopen_409_and_existing_channel_untouched():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    assert open_channel(client, sid).status_code == 200
    resp = open_channel(client, sid)
    assert resp.status_code == 409
    # 已有通道不受重复开启影响：事件仍从 seq 1 起连续编号
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
    ]


def test_events_before_open_are_not_recorded():
    """开启之前产生的事件不补记；开启响应的 active 即初始可见集合。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # s2 的 800 进入发生在开启之前
    client.post(f"/sessions/{sid}/events", json={"time_ms": 900})
    body = open_channel(client, sid).json()
    assert [a["id"] for a in body["active"]] == ["s1", "s2"]

    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 1000, "exit", "s1"),
        (2, 1000, "enter", "s3"),
    ]


def test_open_unknown_or_finished_session_404():
    client = TestClient(app)
    assert open_channel(client, "no-such-session").status_code == 404
    sid = make_session(client)["session_id"]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert open_channel(client, sid).status_code == 404


# ---------------------------------------------------------------------------
# 记录：三类操作的全部 enter/exit 入队，短字幕不漏，seq 连续
# ---------------------------------------------------------------------------


def test_plain_advance_records_short_subtitle_pair():
    """普通推进响应不含事件，但跨过的短字幕进入/退出完整入队。"""
    client = TestClient(app)
    revise(client, "scene-chan-short", 0, [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000},
        {"id": "b", "speaker": "B", "start_ms": 500, "end_ms": 600},
    ])
    sid = client.post(
        "/sessions", json={"scene_id": "scene-chan-short"}
    ).json()["session_id"]
    assert open_channel(client, sid).status_code == 200

    # 一次普通推进跨过整条短字幕 b：响应仍是原样（无 events 字段）
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000})
    assert resp.status_code == 200
    assert "events" not in resp.json()

    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 500, "enter", "b"),
        (2, 600, "exit", "b"),
        (3, 1000, "exit", "a"),
    ]


def test_mixed_operations_enqueue_with_continuous_seq():
    """普通推进、事件推进、隐藏切换的事件共用一条连续序号。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)

    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    # 游标 1000 处 s3 活动：隐藏 Alice 使其可见->隐藏，发 exit（时刻取游标）
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )

    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
        (4, 1000, "exit", "s3"),
    ]
    # 事件字段与事件推进一致
    assert body["events"][0] == {
        "seq": 1, "id": "s2", "speaker": "Bob", "track": 2,
        "time_ms": 800, "type": "enter",
    }


def test_hidden_speakers_events_not_recorded():
    """隐藏说话人的字幕不产生事件，也不会入队。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    open_channel(client, sid)
    # 隐藏 Alice：s1 活动 -> exit@900 入队
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )
    # 隐藏期间推进：Alice 的 s1 退出、s3 进入/退出都被排除，只剩 s2
    client.post(f"/sessions/{sid}/events", json={"time_ms": 3000})
    # 恢复显示：游标 3000 处 Alice 无活动字幕，无 enter 事件
    client.post(
        f"/sessions/{sid}/hidden_speakers", json={"hidden_speakers": []}
    )

    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 900, "exit", "s1"),
        (2, 3000, "exit", "s2"),
    ]


def test_accept_does_not_rewrite_queued_events():
    """接纳场景修订不重写已排队内容；新事件在新快照上继续递增编号。"""
    client = TestClient(app)
    scene_id = "scene-chan-accept"
    sid = make_session(client, scene_id)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    # 修订为版本 2：s3 延长到 4000，新增 s4[3200,4000)；会话接纳
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])
    assert client.post(f"/sessions/{sid}/accept").status_code == 200
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1200})

    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 800, "enter", "s2"),    # 接纳前记录的内容原样保留
        (2, 1000, "exit", "s1"),    # 接纳后新快照产生的事件继续编号
        (3, 1000, "enter", "s3"),
    ]


def test_channel_is_per_session():
    client = TestClient(app)
    make_session(client)
    s1 = client.post(
        "/sessions", json={"scene_id": "scene-chan"}
    ).json()["session_id"]
    s2 = client.post(
        "/sessions", json={"scene_id": "scene-chan"}
    ).json()["session_id"]
    assert open_channel(client, s1).status_code == 200
    # 另一会话未开启通道：补取 404；各自独立开启
    assert fetch(client, s2, 0).status_code == 404
    assert open_channel(client, s2).status_code == 200

    client.post(f"/sessions/{s1}/events", json={"time_ms": 1000})
    assert len(fetch(client, s1, 0).json()["events"]) == 3
    assert fetch(client, s2, 0).json()["events"] == []


# ---------------------------------------------------------------------------
# 补取与确认：ack 边界、不回退、重复补取内容不变
# ---------------------------------------------------------------------------


def test_fetch_ack_flow_and_lost_response_retry():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )

    # 丢响应后补取：以上次确认位置 0 重取全部未确认事件
    body = fetch(client, sid, 0).json()
    assert body["acked_seq"] == 0
    assert [e["seq"] for e in body["events"]] == [1, 2, 3, 4]
    # 未确认事件重复补取：内容与序号完全相同
    assert fetch(client, sid, 0).json() == body

    # 确认前两条：返回确认位置与剩余事件（按 seq 递增）
    body = fetch(client, sid, 2).json()
    assert body["acked_seq"] == 2
    assert [e["seq"] for e in body["events"]] == [3, 4]

    # 旧确认不回退：ack 1 小于已确认位置 2，确认位置保持 2
    body = fetch(client, sid, 1).json()
    assert body["acked_seq"] == 2
    assert [e["seq"] for e in body["events"]] == [3, 4]

    # 越界：此前补取最多返回到 seq 4，ack 5 返回 422 且通道不变
    resp = fetch(client, sid, 5)
    assert resp.status_code == 422
    assert resp.json()["detail"]["max_returned_seq"] == 4
    body = fetch(client, sid, 4).json()
    assert body["acked_seq"] == 4
    assert body["events"] == []
    # 全部确认后再次补取：确认位置不变、无剩余事件
    assert fetch(client, sid, 0).json() == {
        "session_id": sid,
        "scene_id": "scene-chan",
        "acked_seq": 4,
        "events": [],
    }


def test_ack_beyond_returned_422_even_if_events_queued():
    """只能确认此前补取已返回的最大 seq：未补取过的事件不能确认。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    # 事件已入队但从未补取返回过：已返回的最大 seq 仍为 0
    assert fetch(client, sid, 1).status_code == 422
    # 先补取看到事件，再确认
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [
        1, 2, 3,
    ]
    body = fetch(client, sid, 3).json()
    assert body["acked_seq"] == 3
    assert body["events"] == []


def test_new_events_extend_after_ack():
    """确认后新产生的事件继续递增编号，补取只返回未确认部分。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    body = fetch(client, sid, 0).json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert fetch(client, sid, 3).json()["acked_seq"] == 3

    # 隐藏切换再产生一条事件：seq 4；补取只返回它
    client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Bob"]},
    )
    body = fetch(client, sid, 3).json()
    assert _quads(body) == [(4, 1000, "exit", "s2")]
    # 此时已返回到 seq 4，可以确认
    assert fetch(client, sid, 4).json()["events"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"ack_seq": -1},        # 负数
        {"ack_seq": True},      # 布尔不隐式转换
        {"ack_seq": False},
        {"ack_seq": "1"},       # 字符串不隐式转换
        {"ack_seq": "0"},
        {"ack_seq": 1.5},       # 浮点
        {"ack_seq": None},      # null
        {},                     # 字段缺失
    ],
)
def test_ack_seq_validation_422(payload):
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    resp = client.post(f"/sessions/{sid}/channel/fetch", json=payload)
    assert resp.status_code == 422
    # 非法请求不改通道：ack 0 补取仍返回初始状态
    body = fetch(client, sid, 0).json()
    assert body["acked_seq"] == 0
    assert body["events"] == []


def test_fetch_without_open_404():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    assert fetch(client, sid, 0).status_code == 404


def test_fetch_unknown_session_404():
    client = TestClient(app)
    assert fetch(client, "no-such-session", 0).status_code == 404


# ---------------------------------------------------------------------------
# 结束清除通道；旧接口响应不变；失败不改会话或通道
# ---------------------------------------------------------------------------


def test_finish_clears_channel():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    # 结束后补取与开启一律 404
    assert fetch(client, sid, 0).status_code == 404
    assert open_channel(client, sid).status_code == 404


def test_existing_responses_unchanged_with_channel_open():
    """开启通道后，既有接口的响应字段保持原样（事件不带 seq）。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)

    body = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 900}
    ).json()
    assert set(body) == {
        "session_id", "scene_id", "source_revision", "cursor_ms", "active",
    }

    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 1000}
    ).json()
    assert set(body) == {
        "session_id", "scene_id", "source_revision", "cursor_ms",
        "events", "active",
    }
    assert all("seq" not in e for e in body["events"])

    body = client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    ).json()
    assert set(body) == {
        "session_id", "scene_id", "source_revision", "cursor_ms",
        "hidden_speakers", "events", "active",
    }
    assert all("seq" not in e for e in body["events"])


def test_failed_operations_enqueue_nothing():
    """失败的推进/隐藏切换不改会话也不改通道。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    # 游标倒退 422：不入队、游标不变
    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 900})
    assert resp.status_code == 422
    # 非法隐藏集合 422：不入队、集合不变
    resp = client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["A", "A"]},
    )
    assert resp.status_code == 422

    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
    ]
    state = client.post(f"/sessions/{sid}/finish").json()
    assert state["cursor_ms"] == 1000


# ---------------------------------------------------------------------------
# 串行化
# ---------------------------------------------------------------------------


def test_concurrent_open_exactly_one_succeeds():
    """并发开启在会话锁内串行：恰好一次 200，其余 409。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]

    results = []
    barrier = threading.Barrier(6)

    def worker():
        local = TestClient(app)
        barrier.wait()
        results.append(open_channel(local, sid).status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409, 409, 409, 409, 409]
    # 通道正常可用
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [
        1, 2, 3,
    ]


def test_concurrent_fetches_return_identical_unacked_events():
    """并发补取未确认事件：每个响应的内容与序号完全一致。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 3000})

    bodies = []
    barrier = threading.Barrier(4)

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = fetch(local, sid, 0)
        assert resp.status_code == 200
        bodies.append(resp.json())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(bodies) == 4
    for body in bodies[1:]:
        assert body == bodies[0]
    assert _quads(bodies[0]) == [
        (1, 800, "enter", "s2"),
        (2, 1000, "exit", "s1"),
        (3, 1000, "enter", "s3"),
        (4, 2500, "exit", "s3"),
        (5, 3000, "exit", "s2"),
    ]


def test_fetch_serialized_with_finish():
    """补取与结束串行：结束后才轮到生效的补取返回 404。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    open_channel(client, sid)
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert fetch(client, sid, 0).status_code == 404
