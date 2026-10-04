"""场景修订/读取接口测试：持久化、版本乐观锁、无解不保存、并发只成功一次。"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def payload(start=0, end=1000, locked=None):
    sub = {"id": "s1", "speaker": "Alice", "start_ms": start, "end_ms": end}
    if locked is not None:
        sub["locked_track"] = locked
    return {
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            sub,
            {"id": "s2", "speaker": "Bob", "start_ms": 500, "end_ms": 1500},
        ],
    }


def test_create_scene_returns_revision_1():
    client = TestClient(app)
    resp = client.put("/scenes/scene-1", json=payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["revision"] == 1
    assert body["scene_id"] == "scene-1"
    assert body["track_count"] == 2
    assert body["changes"] == 0
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
    ]
    assert body["subtitles"][0]["id"] == "s1"
    assert body["subtitles"][0]["locked_track"] is None


def test_read_scene_after_create():
    client = TestClient(app)
    client.put("/scenes/scene-read", json=payload())
    resp = client.get("/scenes/scene-read")
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 1
    assert body["changes"] == 0
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
    ]
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2"]


def test_read_unknown_scene_404():
    client = TestClient(app)
    assert client.get("/scenes/nope").status_code == 404


def test_revision_increments_and_read_returns_latest():
    client = TestClient(app)
    # 初次创建：revision 1
    r1 = client.put("/scenes/scene-rev", json=payload())
    assert r1.json()["revision"] == 1

    # 提交带锁定的新版本（沿用同一校验/求解规则），基于 revision=1
    body2 = payload(locked=1)
    body2["expected_revision"] = 1
    r2 = client.put("/scenes/scene-rev", json=body2)
    assert r2.status_code == 200
    assert r2.json()["revision"] == 2
    assert r2.json()["subtitles"][0]["locked_track"] == 1

    # 再修订到 revision 3
    body3 = payload(end=900)
    body3["expected_revision"] = 2
    r3 = client.put("/scenes/scene-rev", json=body3)
    assert r3.status_code == 200
    assert r3.json()["revision"] == 3

    got = client.get("/scenes/scene-rev").json()
    assert got["revision"] == 3
    assert got["subtitles"][0]["end_ms"] == 900
    # 读取结果是某次提交的完整快照，不混用旧数据
    assert got["subtitles"][0]["locked_track"] is None


def test_stale_revision_returns_409_and_keeps_old_version():
    client = TestClient(app)
    client.put("/scenes/scene-conflict", json=payload())  # revision=1

    stale = payload(end=900)
    stale["expected_revision"] = 0  # 已存在场景却按新场景提交
    resp = client.put("/scenes/scene-conflict", json=stale)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["current_revision"] == 1

    # 另一处常见的过期版本：落后一版
    stale["expected_revision"] = 1
    # 先推进到 revision 2
    newer = payload(end=900)
    newer["expected_revision"] = 1
    assert client.put("/scenes/scene-conflict", json=newer).status_code == 200
    resp = client.put("/scenes/scene-conflict", json=stale)
    assert resp.status_code == 409

    # 已有版本未被失败的提交改动
    got = client.get("/scenes/scene-conflict").json()
    assert got["revision"] == 2
    assert got["subtitles"][0]["end_ms"] == 900


def test_invalid_input_422_does_not_touch_version():
    client = TestClient(app)
    # 新场景直接提交非法输入：422，场景不应被创建
    bad = {
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100},
            {"id": "b", "speaker": "A", "start_ms": 50, "end_ms": 150},
        ],
    }
    resp = client.put("/scenes/scene-bad", json=bad)
    assert resp.status_code == 422
    assert client.get("/scenes/scene-bad").status_code == 404

    # 已存在场景收到非法输入：422，版本不变
    client.put("/scenes/scene-bad2", json=payload())
    bad2 = dict(bad)
    bad2["expected_revision"] = 1
    resp = client.put("/scenes/scene-bad2", json=bad2)
    assert resp.status_code == 422
    assert client.get("/scenes/scene-bad2").json()["revision"] == 1


def test_missing_expected_revision_is_422():
    client = TestClient(app)
    body = payload()
    del body["expected_revision"]
    resp = client.put("/scenes/scene-x", json=body)
    assert resp.status_code == 422


def test_expected_revision_negative_is_422():
    client = TestClient(app)
    body = payload()
    body["expected_revision"] = -1
    assert client.put("/scenes/scene-x", json=body).status_code == 422


def test_infeasible_returns_assign_style_response_and_is_not_saved():
    client = TestClient(app)
    body = {
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100,
             "locked_track": 1},
            {"id": "b", "speaker": "B", "start_ms": 50, "end_ms": 150,
             "locked_track": 1},
        ],
    }
    resp = client.put("/scenes/scene-infeasible", json=body)
    assert resp.status_code == 200
    # 与 /assign 无解响应完全一致
    assert resp.json() == {"feasible": False, "changes": 0, "assignments": []}
    # 无解不保存
    assert client.get("/scenes/scene-infeasible").status_code == 404


def test_infeasible_revision_does_not_change_existing_scene():
    client = TestClient(app)
    client.put("/scenes/scene-if2", json=payload())  # revision=1
    body = {
        "track_count": 2,
        "expected_revision": 1,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100,
             "locked_track": 1},
            {"id": "b", "speaker": "B", "start_ms": 50, "end_ms": 150,
             "locked_track": 1},
        ],
    }
    resp = client.put("/scenes/scene-if2", json=body)
    assert resp.json() == {"feasible": False, "changes": 0, "assignments": []}
    got = client.get("/scenes/scene-if2").json()
    assert got["revision"] == 1
    assert [s["id"] for s in got["subtitles"]] == ["s1", "s2"]


def test_blank_scene_id_is_422():
    client = TestClient(app)
    resp = client.put("/scenes/%20%20", json=payload())
    assert resp.status_code == 422


def test_concurrent_revisions_same_version_only_one_succeeds():
    client = TestClient(app)
    client.put("/scenes/scene-race", json=payload())  # 当前 revision=1

    results = []
    barrier = threading.Barrier(5)

    def worker(end_ms):
        local = TestClient(app)
        body = payload(end=end_ms)
        body["expected_revision"] = 1  # 5 个线程都基于 revision=1
        barrier.wait()
        resp = local.put("/scenes/scene-race", json=body)
        results.append((end_ms, resp.status_code, resp.json()))

    threads = [
        threading.Thread(target=worker, args=(800 + i * 10,)) for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r[1] == 200]
    conflicts = [r for r in results if r[1] == 409]
    assert len(successes) == 1
    assert len(conflicts) == 4
    assert successes[0][2]["revision"] == 2
    for _, _, body in conflicts:
        assert body["detail"]["current_revision"] == 2

    # 库里的字幕/分轨只来自唯一获胜的那次提交，没有混用
    winner_end = successes[0][0]
    got = client.get("/scenes/scene-race").json()
    assert got["revision"] == 2
    assert got["subtitles"][0]["end_ms"] == winner_end


def test_concurrent_create_same_new_scene_only_one_succeeds():
    results = []
    barrier = threading.Barrier(4)

    def worker(end_ms):
        local = TestClient(app)
        body = payload(end=end_ms)
        body["expected_revision"] = 0
        barrier.wait()
        resp = local.put("/scenes/scene-new-race", json=body)
        results.append((end_ms, resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker, args=(700 + i * 10,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r[1] == 200]
    conflicts = [r for r in results if r[1] == 409]
    assert len(successes) == 1
    assert len(conflicts) == 3
    assert successes[0][2]["revision"] == 1
    for _, _, body in conflicts:
        assert body["detail"]["current_revision"] == 1


def test_persistence_reopen_store_same_path(tmp_path):
    """模拟容器重启：用同一数据库路径重新打开存储，场景仍可读。"""
    import os
    from app.storage import SceneStore

    db_path = os.environ["SCENES_DB_PATH"]
    client = TestClient(app)
    client.put("/scenes/scene-persist", json=payload())

    reopened = SceneStore(db_path)
    record = reopened.get_scene("scene-persist")
    assert record is not None
    assert record.revision == 1
    assert record.changes == 0
    assert [s["id"] for s in record.subtitles] == ["s1", "s2"]
    assert record.assignments == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
    ]
