"""HTTP 层测试：需要安装 requirements-dev.txt（pytest 自动收集）。"""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_assign_feasible():
    resp = client.post("/assign", json={
        "track_count": 2,
        "subtitles": [
            {"id": 1, "speaker": "A", "start_ms": 0, "end_ms": 1000},
            {"id": 2, "speaker": "B", "start_ms": 500, "end_ms": 1500},
            {"id": 3, "speaker": "A", "start_ms": 1000, "end_ms": 2000},
        ],
    })
    assert resp.status_code == 200
    assert resp.json() == {
        "feasible": True,
        "changes": 0,
        "assignments": [
            {"id": 1, "track": 1},
            {"id": 2, "track": 2},
            {"id": 3, "track": 1},
        ],
    }


def test_assign_infeasible_empty_solution():
    resp = client.post("/assign", json={
        "track_count": 2,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100,
             "locked_track": 1},
            {"id": "b", "speaker": "B", "start_ms": 50, "end_ms": 150,
             "locked_track": 1},
        ],
    })
    assert resp.status_code == 200
    assert resp.json() == {"feasible": False, "changes": 0, "assignments": []}


def test_422_on_same_speaker_overlap():
    resp = client.post("/assign", json={
        "track_count": 2,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100},
            {"id": "b", "speaker": "A", "start_ms": 50, "end_ms": 150},
        ],
    })
    assert resp.status_code == 422


def test_422_on_bad_interval_and_range():
    resp = client.post("/assign", json={
        "track_count": 4,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 100, "end_ms": 100},
        ],
    })
    assert resp.status_code == 422


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
