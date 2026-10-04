"""补取通道共享容量（CHANNEL_EVENT_LIMIT）：全进程未确认事件总量上限。

覆盖：
- 配置：缺省 1024；正整数可经环境变量配置（Compose 同一变量）；
  0、负数、小数、非数字、带符号/下划线等非法配置拒绝启动（导入即失败）；
- 生产申请：普通推进、事件推进、隐藏切换按本次实际事件数占用共享容量，
  与游标推进/隐藏集合替换/seq 分配/完整入队一同生效；不足立即 503，
  不推进游标、不改变隐藏集合、不消费 seq、不部分入队；
- 无事件操作与未开启通道在满额时仍可执行；接纳不占容量、不改队列；
- 释放：只有确认并删除前缀才按删除数释放，纯读取与旧确认不释放；
  结束释放剩余队列，重复/并发结束不重复释放；释放后原请求可重试；
- 跨会话：多个会话的未确认事件合计不得突破总额；确认/结束/生产
  并发不死锁、不泄漏容量。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.playback import (  # noqa: E402
    DEFAULT_CHANNEL_EVENT_LIMIT,
    ChannelCapacity,
    ChannelCapacityExhausted,
    ChannelEventLimitInvalid,
    PlaybackManager,
    parse_channel_event_limit,
)


def scene_payload(scene_id="scene-cap"):
    # s1[0,1000) s2[800,3000) s3[1000,2500)：事件推进到 1000 产生 3 条
    return {
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ],
    }


def make_client_with_limit(monkeypatch, limit):
    """配置 CHANNEL_EVENT_LIMIT 并重建惰性播放管理器，返回全新 TestClient。"""
    monkeypatch.setenv("CHANNEL_EVENT_LIMIT", str(limit))
    main_module.reset_playback()
    return TestClient(app)


def make_session(client, scene_id="scene-cap"):
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def open_channel(client, sid):
    return client.post(f"/sessions/{sid}/channel")


def fetch(client, sid, ack_seq):
    return client.post(
        f"/sessions/{sid}/channel/fetch", json={"ack_seq": ack_seq}
    )


def capacity(client=None):
    return main_module.get_playback().channel_capacity


def _quads(body):
    return [
        (e["seq"], e["time_ms"], e["type"], e["id"]) for e in body["events"]
    ]


# ---------------------------------------------------------------------------
# 配置：默认值、正整数可配、非法拒绝启动
# ---------------------------------------------------------------------------


def test_default_limit_is_1024(monkeypatch):
    monkeypatch.delenv("CHANNEL_EVENT_LIMIT", raising=False)
    main_module.reset_playback()
    cap = capacity()
    assert cap.limit == DEFAULT_CHANNEL_EVENT_LIMIT == 1024
    assert cap.used == 0
    assert cap.remaining == 1024


@pytest.mark.parametrize("raw,expected", [
    (None, 1024),
    ("1", 1),
    ("1024", 1024),
    ("  2048  ", 2048),
    ("999999999999", 999999999999),
    ("  7 ", 7),
])
def test_parse_channel_event_limit_valid(raw, expected):
    assert parse_channel_event_limit(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "\t\n",
    "0",
    "000",
    "-1",
    "-1024",
    "+1",
    "1.5",
    "1e3",
    "abc",
    "1_000",
    "0x10",
    "true",
    "１２",          # 全角数字不是 ASCII 数字
    "12 a",
])
def test_parse_channel_event_limit_invalid(raw):
    with pytest.raises(ValueError):
        parse_channel_event_limit(raw)


def test_invalid_limit_rejects_startup_subprocess(tmp_path):
    """非法配置必须拒绝启动：子进程导入 app 即以非零状态退出。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(main_module.__file__).resolve().parents[1])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CHANNEL_EVENT_LIMIT": "0",
        "SCENES_DB_PATH": str(tmp_path / "startup.db"),
        "PYTHONPATH": repo_root,
    }
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert "CHANNEL_EVENT_LIMIT" in result.stderr


def test_limit_configurable_via_environment(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    assert capacity(client).limit == 3


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "3"])
def test_capacity_rejects_non_positive_integer_limit(bad):
    # 直接构造容量对象的上限必须为正整数（含拒绝布尔值）
    with pytest.raises(ChannelEventLimitInvalid):
        ChannelCapacity(bad)


def test_capacity_unit_reserve_release_guards():
    cap = ChannelCapacity(2)
    cap.reserve(2)
    assert cap.remaining == 0
    # 满额：再申请 1 个原子失败且不占用
    with pytest.raises(ChannelCapacityExhausted) as exc:
        cap.reserve(1)
    assert exc.value.requested == 1
    assert cap.used == 2
    # 0 个名额在满额时仍可申请
    cap.reserve(0)
    # 释放 1 个后原申请成功
    cap.release(1)
    cap.reserve(1)
    assert cap.used == 2
    cap.release(2)
    assert cap.used == 0
    # 超额释放（记账与删除必须成对）直接报错，容量不得变负
    with pytest.raises(RuntimeError):
        cap.release(1)
    assert cap.used == 0


def test_manager_rejects_explicit_invalid_limit():
    with pytest.raises(ChannelEventLimitInvalid):
        PlaybackManager(channel_event_limit=0)


# ---------------------------------------------------------------------------
# 满额：生产按实际事件数申请，不足 503 且无任何部分生效
# ---------------------------------------------------------------------------


def test_exact_capacity_succeeds_then_next_production_503(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    sid = make_session(client)
    open_channel(client, sid)

    # 恰好 3 条事件，占满全部共享容量
    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert resp.status_code == 200
    cap = capacity()
    assert cap.used == 3
    assert cap.remaining == 0

    # 再来 1 条事件都申请不到：503，detail 给出限制与缺口
    resp = client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["limit"] == 3
    assert detail["requested"] == 1
    assert detail["available"] == 0


def test_503_advances_no_cursor_consumes_no_seq(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    sid = make_session(client)
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    # 事件推进到 2500 会新增 s3 的 2500 退出（第 4 条），容量不足
    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 2500})
    assert resp.status_code == 503
    # 游标停在 1000
    assert fetch(client, sid, 0).json()["acked_seq"] == 0
    body = fetch(client, sid, 0).json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert capacity().used == 3
    # 会话自身游标未推进：finish 快照游标仍为 1000
    finished = client.post(f"/sessions/{sid}/finish").json()
    assert finished["cursor_ms"] == 1000


def test_503_plain_advance_keeps_cursor_and_queue(monkeypatch):
    """普通推进满额 503：游标不推进、不部分入队。"""
    client = make_client_with_limit(monkeypatch, 3)
    sid = make_session(client)
    open_channel(client, sid)
    # 占满 3 条
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 1000}
    ).status_code == 200
    # 推进到 3000 还会产生 2 条（s3@2500 exit、s2@3000 exit），容量不足
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 3000})
    assert resp.status_code == 503
    # active 仍反映游标 1000（s2、s3 活动）
    active = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 1000}
    ).json()["active"]
    assert sorted(a["id"] for a in active) == ["s2", "s3"]
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [
        1, 2, 3,
    ]


def test_503_hidden_switch_does_not_change_set(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    sid = make_session(client)
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    # 隐藏 Alice 会对活动的 s3 产生 1 条 exit：容量不足
    resp = client.post(
        f"/sessions/{sid}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    )
    assert resp.status_code == 503
    # 隐藏集合未改变：再做无事件的等时事件推进，Alice 的 s3 仍在 active 中
    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 1000}
    ).json()
    assert sorted(a["id"] for a in body["active"]) == ["s2", "s3"]
    # 队列中也没有出现第 4 条
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [
        1, 2, 3,
    ]


def test_503_requested_is_whole_batch_no_partial_enqueue(monkeypatch):
    """批量事件在容量只够一部分时也整体拒绝，不部分入队。"""
    client = make_client_with_limit(monkeypatch, 4)
    sid = make_session(client)
    open_channel(client, sid)
    # 先占用 3 条（剩余 1 个名额）
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert capacity().remaining == 1
    # 下一批 2 条（s3@2500、s2@3000）：只够 1 条也必须整体 503
    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 3000})
    assert resp.status_code == 503
    assert resp.json()["detail"]["requested"] == 2
    assert resp.json()["detail"]["available"] == 1
    assert capacity().used == 3
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [
        1, 2, 3,
    ]


# ---------------------------------------------------------------------------
# 无事件操作与未开启通道：满额时不占容量、仍可执行
# ---------------------------------------------------------------------------


def test_no_event_operations_still_work_when_full(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    sid = make_session(client)
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    assert capacity().remaining == 0

    # 等时推进（0 个事件）：普通推进与事件推进均 200
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 1000}
    ).status_code == 200
    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 1000}
    ).json()
    assert body["events"] == []
    # 隐藏集合无变化：0 个事件，仍 200，且不占容量
    body = client.post(
        f"/sessions/{sid}/hidden_speakers", json={"hidden_speakers": []}
    ).json()
    assert body["events"] == []
    # 读取（fetch ack 0，纯补取不确认）：200 且不释放、不新增容量
    assert fetch(client, sid, 0).status_code == 200
    assert capacity().used == 3


def test_sessions_without_channel_consume_no_capacity(monkeypatch):
    client = make_client_with_limit(monkeypatch, 1)
    # 会话 A 开启通道，游标 0 处隐藏 Alice：s1 活动产生 1 条 exit，
    # 占满唯一名额（隐藏集合先不动游标，恰好 1 个事件）
    a = make_session(client, "scene-cap-a")
    open_channel(client, a)
    assert client.post(
        f"/sessions/{a}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    ).status_code == 200
    assert capacity().used == 1

    # 会话 B 未开启通道：任意推进都不占容量，也不会 503
    b = make_session(client, "scene-cap-b")
    assert client.post(
        f"/sessions/{b}/events", json={"time_ms": 3000}
    ).status_code == 200
    assert client.post(
        f"/sessions/{b}/advance", json={"time_ms": 3000}
    ).status_code == 200
    assert client.post(
        f"/sessions/{b}/hidden_speakers",
        json={"hidden_speakers": ["Alice"]},
    ).status_code == 200
    assert capacity().used == 1

    # B 之后再开启通道：开启本身不占容量
    assert open_channel(client, b).status_code == 200
    assert capacity().used == 1
    # 但 B 开启后生产同样受总额约束：再推进到 3000（游标 3000 已是末点，
    # 改在新会话 C 上验证更直接）——这里用 C：未开启时推进不占容量，
    # 开启后的第一次生产即 503
    c = make_session(client, "scene-cap-c")
    assert client.post(
        f"/sessions/{c}/events", json={"time_ms": 1000}
    ).status_code == 200
    assert open_channel(client, c).status_code == 200
    assert capacity().used == 1
    # 开启不补记、不占容量；开启后推进产生 3 条事件：满额 503
    resp = client.post(f"/sessions/{c}/events", json={"time_ms": 3000})
    assert resp.status_code == 503
    # C 游标仍为开启前推进到的 1000
    assert client.post(f"/sessions/{c}/finish").json()["cursor_ms"] == 1000
    client.post(f"/sessions/{a}/finish")
    client.post(f"/sessions/{b}/finish")
    assert capacity().used == 0


def test_accept_does_not_consume_or_release_capacity(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    scene_id = "scene-cap-accept"
    sid = make_session(client, scene_id)
    open_channel(client, sid)
    # 游标推进到 900：s1、s2 已开始，s3（1000 起）尚未开始
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    assert capacity().used == 1

    # 接纳修订（延长未开始的 s3、新增 s4）：不占容量、不重写队列
    resp = client.put(f"/scenes/{scene_id}", json={
        "track_count": 2,
        "expected_revision": 1,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
            {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
        ],
    })
    assert resp.status_code == 200
    assert client.post(f"/sessions/{sid}/accept").status_code == 200
    assert capacity().used == 1
    body = fetch(client, sid, 0).json()
    assert _quads(body) == [
        (1, 800, "enter", "s2"),
    ]
    client.post(f"/sessions/{sid}/finish")
    assert capacity().used == 0


# ---------------------------------------------------------------------------
# 释放：确认前缀释放；读取与旧确认不释放；结束释放且不重复
# ---------------------------------------------------------------------------


def test_ack_prefix_releases_capacity_and_enables_retry(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    sid = make_session(client)
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})
    # 满额后下一批 2 条被 503
    assert client.post(
        f"/sessions/{sid}/events", json={"time_ms": 3000}
    ).status_code == 503

    # 确认并删除前 2 条：先补取返回（建立“已返回”上界），再确认前缀
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [
        1, 2, 3,
    ]
    body = fetch(client, sid, 2).json()
    assert body["acked_seq"] == 2
    assert [e["seq"] for e in body["events"]] == [3]
    assert capacity().used == 1
    assert capacity().remaining == 2

    # 原请求重试：完整成功，游标推进、新事件按既有 seq 继续编号
    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 3000})
    assert resp.status_code == 200
    assert capacity().used == 3
    body = fetch(client, sid, 2).json()
    assert _quads(body) == [
        (3, 1000, "enter", "s3"),
        (4, 2500, "exit", "s3"),
        (5, 3000, "exit", "s2"),
    ]


def test_read_and_old_ack_do_not_release(monkeypatch):
    client = make_client_with_limit(monkeypatch, 3)
    sid = make_session(client)
    open_channel(client, sid)
    client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})

    # 纯读取（ack 0）不释放
    fetch(client, sid, 0)
    assert capacity().used == 3
    # 旧确认（ack 1 先确认 1 条，再用更小的 ack 0）不回退、不重复释放
    fetch(client, sid, 1)
    assert capacity().used == 2
    fetch(client, sid, 0)
    assert capacity().used == 2
    # 重复同一确认位置也不释放（前缀此前已删除）
    body = fetch(client, sid, 1).json()
    assert body["acked_seq"] == 1
    assert capacity().used == 2


def test_finish_releases_remaining_queue_once(monkeypatch):
    client = make_client_with_limit(monkeypatch, 5)
    sid = make_session(client)
    open_channel(client, sid)
    assert client.post(f"/sessions/{sid}/events", json={"time_ms": 1000})  # 3 条
    # 先补取返回全部 3 条，再确认 1 条（删除 1），剩余队列 2 条
    assert [e["seq"] for e in fetch(client, sid, 0).json()["events"]] == [
        1, 2, 3,
    ]
    fetch(client, sid, 1)
    assert capacity().used == 2

    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert capacity().used == 0

    # 重复/并发结束不得重复释放：已结束，后续 finish 均 404，used 保持 0
    assert client.post(f"/sessions/{sid}/finish").status_code == 404
    assert capacity().used == 0

    # 新会话在全部容量可用时可从头生产
    sid2 = make_session(client, "scene-cap-again")
    open_channel(client, sid2)
    assert client.post(
        f"/sessions/{sid2}/events", json={"time_ms": 1000}
    ).status_code == 200
    assert capacity().used == 3


def test_finish_without_channel_releases_nothing_and_never_negative(monkeypatch):
    client = make_client_with_limit(monkeypatch, 2)
    sid = make_session(client)
    # 未开启通道直接结束：used 恒为 0，不触发任何释放
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert capacity().used == 0


# ---------------------------------------------------------------------------
# 跨会话：总额共享
# ---------------------------------------------------------------------------


def test_capacity_is_shared_across_sessions(monkeypatch):
    client = make_client_with_limit(monkeypatch, 4)
    a = make_session(client, "scene-share-a")
    b = make_session(client, "scene-share-b")
    open_channel(client, a)
    open_channel(client, b)

    # A 占 3
    client.post(f"/sessions/{a}/events", json={"time_ms": 1000})
    assert capacity().used == 3
    # B 生产 3 条：全进程只剩 1 个名额 -> 503
    resp = client.post(f"/sessions/{b}/events", json={"time_ms": 1000})
    assert resp.status_code == 503
    assert resp.json()["detail"]["available"] == 1
    # B 的会话状态完全未变（游标仍 0）
    assert capacity().used == 3

    # A 先补取返回，再确认全部 3 条后，B 的原请求可成功
    assert [e["seq"] for e in fetch(client, a, 0).json()["events"]] == [
        1, 2, 3,
    ]
    assert fetch(client, a, 3).json()["events"] == []
    assert capacity().used == 0
    assert client.post(
        f"/sessions/{b}/events", json={"time_ms": 1000}
    ).status_code == 200
    assert capacity().used == 3
    # B 结束释放，容量归零
    client.post(f"/sessions/{b}/finish")
    assert capacity().used == 0


# ---------------------------------------------------------------------------
# 并发：生产不突破总额；确认/结束/生产不死锁、不泄漏
# ---------------------------------------------------------------------------


def test_concurrent_production_never_exceeds_limit(monkeypatch):
    limit = 20
    make_client_with_limit(monkeypatch, limit)
    # 重建后用全新 client 构造 4 个各含 12 条短字幕（互不同说话人）的会话，
    # 每个会话一次推进产生 24 条事件（12 进 12 出）。
    main_module.reset_playback()
    client = TestClient(app)
    sessions = []
    for i in range(4):
        name = f"scene-burst-{i}"
        subs = [
            {"id": f"n{j}", "speaker": f"S{j}",
             "start_ms": j * 10, "end_ms": j * 10 + 5}
            for j in range(12)
        ]
        resp = client.put(f"/scenes/{name}", json={
            "track_count": 2,
            "expected_revision": 0,
            "subtitles": subs,
        })
        assert resp.status_code == 200
        sid = client.post(
            "/sessions", json={"scene_id": name}
        ).json()["session_id"]
        open_channel(client, sid)
        sessions.append(sid)

    statuses = []
    barrier = threading.Barrier(len(sessions))

    def worker(sid):
        local = TestClient(app)
        barrier.wait()
        resp = local.post(
            f"/sessions/{sid}/events", json={"time_ms": 100000}
        )
        statuses.append((sid, resp.status_code))

    threads = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cap = capacity()
    # 入队事件总量绝不超过上限；503 的会话不占容量
    assert cap.used <= limit
    # 每个会话需要 24 条 > 上限 20：并发下全部整批 503，无部分入队
    assert all(code == 503 for _, code in statuses)
    rejected = {sid for sid, code in statuses if code == 503}
    assert rejected == set(sessions)
    # 被 503 的会话游标仍是 0（未推进、未消费 seq）
    for sid in sessions:
        gone = client.post(f"/sessions/{sid}/finish")
        assert gone.status_code == 200
        assert gone.json()["cursor_ms"] == 0
    # 全部结束后容量归零，无泄漏
    assert cap.used == 0


def test_concurrent_acks_and_finish_do_not_leak(monkeypatch):
    """并发确认、结束、生产交错：最终全部结束后容量归零，且过程不越限。"""
    limit = 50
    client = make_client_with_limit(monkeypatch, limit)

    def build(idx):
        name = f"scene-mix-{idx}"
        subs = [
            {"id": f"e{j}", "speaker": "P",
             "start_ms": j * 10, "end_ms": j * 10 + 5}
            for j in range(8)
        ]
        client.put(f"/scenes/{name}", json={
            "track_count": 2,
            "expected_revision": 0,
            "subtitles": subs,
        })
        sid = client.post(
            "/sessions", json={"scene_id": name}
        ).json()["session_id"]
        open_channel(client, sid)
        client.post(f"/sessions/{sid}/events", json={"time_ms": 100})
        return sid

    sessions = [build(i) for i in range(6)]
    cap = capacity()
    assert cap.used <= limit

    stop = threading.Event()

    def producer(sid):
        local = TestClient(app)
        while not stop.is_set():
            r = local.post(
                f"/sessions/{sid}/events", json={"time_ms": 500}
            )
            assert r.status_code in (200, 404, 503)
            assert cap.used <= limit

    def acker(sid):
        local = TestClient(app)
        while not stop.is_set():
            r = local.post(
                f"/sessions/{sid}/channel/fetch", json={"ack_seq": 0}
            )
            assert r.status_code in (200, 404)

    threads = []
    for sid in sessions:
        threads.append(threading.Thread(target=producer, args=(sid,)))
        threads.append(threading.Thread(target=acker, args=(sid,)))
    for t in threads:
        t.start()

    # 主线程陆续结束会话
    for sid in sessions:
        client.post(f"/sessions/{sid}/finish")
    stop.set()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "确认/结束/生产发生死锁"

    assert cap.used == 0


def test_503_retry_after_finish_of_other_session(monkeypatch):
    """结束其他会话释放容量后，被 503 的请求可重试。"""
    client = make_client_with_limit(monkeypatch, 3)
    a = make_session(client, "scene-rel-a")
    b = make_session(client, "scene-rel-b")
    open_channel(client, a)
    open_channel(client, b)
    client.post(f"/sessions/{a}/events", json={"time_ms": 1000})
    assert capacity().used == 3
    # B 的生产被 A 的未确认事件挤爆
    assert client.post(
        f"/sessions/{b}/events", json={"time_ms": 1000}
    ).status_code == 503
    # 结束 A：全部容量归还
    client.post(f"/sessions/{a}/finish")
    assert capacity().used == 0
    # B 的原请求重试成功
    assert client.post(
        f"/sessions/{b}/events", json={"time_ms": 1000}
    ).status_code == 200
    assert capacity().used == 3
    client.post(f"/sessions/{b}/finish")
    assert capacity().used == 0
