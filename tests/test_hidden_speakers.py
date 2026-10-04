"""播放时临时隐藏指定说话人字幕的测试。

覆盖：
- 默认不隐藏；按 speaker 精确匹配；空数组清除；允许尚未出现的说话人；
- 切换以当前游标的左闭右开活动区间判定 exit/enter：先 exit 后 enter，
  同类按会话字幕顺序，事件时刻为当前游标，无变化则无事件；
- 设置后 /advance 的 active、/events 的 active 与 events 均排除隐藏者，
  游标照常推进；隐藏期间整条错过的字幕在恢复显示时不补发事件；
- 接纳（accept）保留隐藏集合；完整快照（创建/接纳/finish）与分轨不受影响；
- 切换与推进、事件推进、接纳、结束串行；未知或已结束会话 404，
  非法集合 422 且失败不改状态；设置仅内存态、不持久化。
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


def make_session(client, scene_id="scene-hidden"):
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()


def hide(client, sid, speakers):
    return client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": list(speakers)},
    )


def _triples(body):
    return [(e["time_ms"], e["type"], e["id"]) for e in body["events"]]


# ---------------------------------------------------------------------------
# 默认行为与切换事件
# ---------------------------------------------------------------------------


def test_default_no_hidden_active_includes_everyone():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 900}).json()
    assert [a["id"] for a in body["active"]] == ["s1", "s2"]


def test_hide_active_speaker_emits_exit_at_current_cursor():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    resp = hide(client, sid, ["Alice"])
    assert resp.status_code == 200
    body = resp.json()
    # 游标不动；s1 在 [0,1000) 内活动且由可见变隐藏 -> exit，时刻取游标
    assert body["cursor_ms"] == 900
    assert body["source_revision"] == 1
    assert body["hidden_speakers"] == ["Alice"]
    assert body["events"] == [
        {"id": "s1", "speaker": "Alice", "track": 1,
         "time_ms": 900, "type": "exit"},
    ]
    # 切换后的可见活动字幕只剩 s2（会话字幕顺序）
    assert [(a["id"], a["track"]) for a in body["active"]] == [("s2", 2)]


def test_hide_at_cursor_zero_exits_subtitle_started_at_zero():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 0：s1 [0,1000) 处于活动区间（左闭），隐藏 Alice 发 0 时刻 exit
    body = hide(client, sid, ["Alice"]).json()
    assert _triples(body) == [(0, "exit", "s1")]
    assert body["active"] == []


def test_hide_speaker_without_active_subtitle_emits_no_event():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # Alice 此刻有活动字幕 s1；Carol 从未在场景中出现
    body = hide(client, sid, ["Carol"]).json()
    assert body["events"] == []
    assert [a["id"] for a in body["active"]] == ["s1", "s2"]
    assert body["hidden_speakers"] == ["Carol"]


def test_hide_not_yet_appeared_speaker_hides_future_subtitles():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 0 预先隐藏 Alice（其字幕 s1 此刻即活动，s3 在 1000 才开始）
    body = hide(client, sid, ["Alice"]).json()
    assert _triples(body) == [(0, "exit", "s1")]

    # 事件推进跨过 s1 退出、s3 进入/退出：全部不出现；s2 的进入/退出保留
    body = client.post(f"/sessions/{sid}/events", json={"time_ms": 3000}).json()
    assert _triples(body) == [
        (800, "enter", "s2"),
        (3000, "exit", "s2"),
    ]
    assert body["active"] == []
    assert body["cursor_ms"] == 3000  # 游标照常推进


def test_exact_speaker_match_is_required():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # "alice" 不等于 "Alice"：不隐藏任何字幕
    body = hide(client, sid, ["alice"]).json()
    assert body["events"] == []
    assert [a["id"] for a in body["active"]] == ["s1", "s2"]


def test_clear_with_empty_array_emits_enter_for_active_hidden():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice", "Bob"])

    resp = hide(client, sid, [])
    assert resp.status_code == 200
    body = resp.json()
    assert body["hidden_speakers"] == []
    # s1、s2 都在当前区间活动，由隐藏恢复可见 -> 两个 enter，按会话顺序
    assert body["events"] == [
        {"id": "s1", "speaker": "Alice", "track": 1,
         "time_ms": 900, "type": "enter"},
        {"id": "s2", "speaker": "Bob", "track": 2,
         "time_ms": 900, "type": "enter"},
    ]
    assert [a["id"] for a in body["active"]] == ["s1", "s2"]


def test_no_change_in_set_emits_no_events():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice"])

    # 同集合重复提交（顺序变化但集合相同）：无事件，active 视图不变
    body = hide(client, sid, ["Alice"]).json()
    assert body["events"] == []
    assert [a["id"] for a in body["active"]] == ["s2"]


def test_exit_before_enter_and_within_type_session_order():
    """混合切换：先全部 exit 后全部 enter；同类按会话字幕顺序。"""
    client = TestClient(app)
    # 3 轨：a[0,200) 轨1 与 c[0,200) 轨3 同时活动；b[100,300) 轨2
    revise(client, "scene-mix", 0, [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 200},
        {"id": "b", "speaker": "B", "start_ms": 100, "end_ms": 300},
        {"id": "c", "speaker": "C", "start_ms": 0, "end_ms": 200},
    ], track_count=3)
    sid = client.post("/sessions", json={"scene_id": "scene-mix"}).json()[
        "session_id"
    ]
    # 游标 0 先隐藏 B（b 尚未开始，无事件）
    assert hide(client, sid, ["B"]).json()["events"] == []
    # 推进到 150：b 的进入事件因隐藏被排除；当前活动 a、b、c（b 不可见）
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 150}).json()
    assert [x["id"] for x in body["active"]] == ["a", "c"]

    # 切换为隐藏 A、C：a、c 退出（按会话顺序），b 进入；时刻同为游标 150
    body = hide(client, sid, ["A", "C"]).json()
    assert _triples(body) == [
        (150, "exit", "a"),
        (150, "exit", "c"),
        (150, "enter", "b"),
    ]
    assert [x["id"] for x in body["active"]] == ["b"]


def test_unhide_after_subtitle_elapsed_emits_no_enter():
    """隐藏期间已整条播过的字幕，恢复显示时不补发。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice"])
    # 推进到 3000：s1 早已结束、s3 开始又结束，全程隐藏
    client.post(f"/sessions/{sid}/events", json={"time_ms": 3000})

    body = hide(client, sid, []).json()
    # 当前游标 3000：Alice 的字幕无一条活动，不产生 enter
    assert body["events"] == []
    assert body["active"] == []


# ---------------------------------------------------------------------------
# 设置后推进 / 事件推进均排除隐藏者，游标照常推进
# ---------------------------------------------------------------------------


def test_advance_active_excludes_hidden_but_cursor_moves():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice"])

    # 1000：s1 结束、s3 开始（均为 Alice，隐藏）；可见活动只剩 s2
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000}).json()
    assert body["cursor_ms"] == 1000
    assert [a["id"] for a in body["active"]] == ["s2"]

    # 清除后同样以当前游标区间为准：s3 在 [1000,2500) 内，恢复可见
    body = hide(client, sid, []).json()
    assert _triples(body) == [(1000, "enter", "s3")]
    assert [a["id"] for a in body["active"]] == ["s2", "s3"]


def test_events_advance_excludes_hidden_subtitles():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice"])

    # 窗口 (900,3000]：s1 的 1000 退出与 s3 的 1000 进入、2500 退出都隐藏；
    # 只剩 s2 的 3000 退出
    body = client.post(f"/sessions/{sid}/events", json={"time_ms": 3000}).json()
    assert _triples(body) == [(3000, "exit", "s2")]
    assert body["active"] == []
    assert body["cursor_ms"] == 3000


def test_hide_does_not_affect_cursor_regression_rules():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice"])
    # 隐藏不改变游标语义：倒退仍 422
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 800})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 接纳保留隐藏集合；完整快照与分轨不受影响
# ---------------------------------------------------------------------------


def test_accept_preserves_hidden_speakers():
    client = TestClient(app)
    scene_id = "scene-hidden"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice"])

    # 修订为版本 2：s3 延长到 4000，新增 s4[3200,4000)
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])
    assert client.post(f"/sessions/{sid}/accept").status_code == 200

    # Alice 仍隐藏：推进到 3500 只见 s2（已结束）与 s4——s3 被排除
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 3500}).json()
    assert body["source_revision"] == 2
    assert [a["id"] for a in body["active"]] == ["s4"]

    # 清除隐藏后 s3 回到活动集合（其分轨仍是接纳后重算的轨道 1）
    body = hide(client, sid, []).json()
    assert _triples(body) == [(3500, "enter", "s3")]
    assert body["events"][0]["track"] == 1
    assert [a["id"] for a in body["active"]] == ["s3", "s4"]


def test_full_snapshot_unaffected_by_hiding():
    """创建/接纳/finish 的完整快照与分轨不随隐藏变化。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Alice", "Bob"])

    state = client.post(f"/sessions/{sid}/finish").json()
    assert "hidden_speakers" not in state
    # 全部字幕与分轨照常出现在最终快照里
    assert [s["id"] for s in state["subtitles"]] == ["s1", "s2", "s3"]
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


# ---------------------------------------------------------------------------
# 错误处理：404 / 422 / 失败不改状态
# ---------------------------------------------------------------------------


def test_unknown_session_404():
    client = TestClient(app)
    assert hide(client, "no-such-session", ["X"]).status_code == 404


def test_finished_session_404():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert hide(client, sid, ["X"]).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"hidden_speakers": ["a", "a"]},      # 重复
        {"hidden_speakers": [""]},            # 空字符串
        {"hidden_speakers": ["a", ""]},       # 含空字符串
        {"hidden_speakers": [1]},             # 数字不隐式转换
        {"hidden_speakers": [True]},          # 布尔不隐式转换
        {"hidden_speakers": [1.5]},           # 浮点
        {"hidden_speakers": [None]},          # null 元素
        {"hidden_speakers": "Alice"},         # 非数组
        {"hidden_speakers": None},            # null
        {},                                   # 字段缺失
    ],
)
def test_invalid_sets_422(payload):
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    resp = client.post(f"/sessions/{sid}/hidden_speakers", json=payload)
    assert resp.status_code == 422


def test_whitespace_only_speaker_is_accepted_as_non_empty():
    """非空字符串即合法：全空白字符串不做空值拦截（按精确 speaker 匹配）。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    resp = hide(client, sid, [" "])
    assert resp.status_code == 200
    assert resp.json()["hidden_speakers"] == [" "]


def test_failed_request_does_not_change_state():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    assert hide(client, sid, ["Alice"]).status_code == 200

    # 非法请求 422：隐藏集合保持为 {"Alice"}
    assert hide(client, sid, ["Bob", "Bob"]).status_code == 422
    body = hide(client, sid, ["Alice"]).json()  # 与现存集合相同
    assert body["events"] == []
    assert [a["id"] for a in body["active"]] == ["s2"]

    # 回显集合仍是上次成功的设置：再非法提交空字符串也不影响
    assert hide(client, sid, [""]).status_code == 422
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000}).json()
    assert [a["id"] for a in body["active"]] == ["s2"]


# ---------------------------------------------------------------------------
# 串行化
# ---------------------------------------------------------------------------


def test_concurrent_toggles_serialized():
    """并发切换在会话锁内串行：全部 200，最终视图与最终集合自洽。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    results = []
    barrier = threading.Barrier(4)

    def worker(speakers):
        local = TestClient(app)
        barrier.wait()
        resp = hide(local, sid, speakers)
        results.append((resp.status_code,
                        resp.json() if resp.status_code == 200 else None))

    plans = [["Alice"], ["Bob"], ["Alice", "Bob"], ["Alice"]]
    threads = [threading.Thread(target=worker, args=(p,)) for p in plans]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 无中间态泄露：全部成功；每个响应的 events 与 active 都与该次切换
    # 实际应用的集合自洽（exit/enter 只涉及状态真正翻转的活动字幕）
    assert all(status == 200 for status, _ in results)
    for _, body in results:
        hidden = set(body["hidden_speakers"])
        for e in body["events"]:
            if e["type"] == "exit":
                assert e["speaker"] in hidden
            else:
                assert e["speaker"] not in hidden
            assert e["time_ms"] == body["cursor_ms"] == 900
        assert all(a["speaker"] not in hidden for a in body["active"])

    # 最终再提交一次确定集合：隐藏 Alice，active 只剩 s2（状态可正常继续）
    body = hide(client, sid, ["Alice"]).json()
    assert [a["id"] for a in body["active"]] == ["s2"]


def test_toggle_serialized_with_finish():
    """切换与结束串行：结束后才轮到生效的切换返回 404。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert hide(client, sid, ["Alice"]).status_code == 404
