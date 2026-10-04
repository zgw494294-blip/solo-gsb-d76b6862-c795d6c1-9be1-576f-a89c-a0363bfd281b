"""场景持久化：本地 SQLite。

一个场景（scene）保存某次成功修订的完整快照：
字幕输入、分轨结果、换轨次数与版本号（revision）。

并发语义采用乐观锁：客户端提交 expected_revision，新场景填 0，
已存在场景须填写当前版本；服务端在一个 BEGIN IMMEDIATE 事务中
读取当前版本并写入新版本，因此同一版本的并发提交最多一次成功，
其余全部以 RevisionConflict 失败且不改动已有数据。

片段另存（clip）与场景拼接（concat）创建新场景时同样在单个
BEGIN IMMEDIATE 事务内核对全部源版本并占用目标：版本核对与目标
创建一同生效，失败不留目标、不改源。

播放存档（playback checkpoint）把某个播放会话的完整播放状态
（快照、游标、隐藏集合、补取通道队列与序号边界、自动播放状态）
按调用方给定的 checkpoint_id 写入同库的 playback_checkpoints 表，
容器重启后仍可读取并据此恢复为全新会话。存档写入与场景写入一样在
BEGIN IMMEDIATE 事务内占用 id：同名存档并发写入最多一次成功，
失败不留存档。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DB_PATH = os.environ.get("SCENES_DB_PATH", "scenes.db")


class RevisionConflict(Exception):
    """expected_revision 与库中当前版本不一致。"""

    def __init__(self, scene_id: str, current_revision: int) -> None:
        self.scene_id = scene_id
        self.current_revision = current_revision
        super().__init__(
            f"场景 {scene_id!r} 的版本冲突：期望新版本基于 "
            f"revision={current_revision} 再次提交"
        )


class TargetSceneExists(Exception):
    """裁剪目标 scene_id 已被占用（含目标与源同名）。"""

    def __init__(self, scene_id: str, current_revision: int) -> None:
        self.scene_id = scene_id
        self.current_revision = current_revision
        super().__init__(
            f"目标场景 {scene_id!r} 已存在（revision={current_revision}），"
            "裁剪目标必须使用未占用的 scene_id"
        )


class SourceSceneMissing(Exception):
    """提交裁剪事务时发现源场景不存在（正常流程中场景不可删除，仅作防御）。"""

    def __init__(self, scene_id: str) -> None:
        self.scene_id = scene_id
        super().__init__(f"源场景 {scene_id!r} 不存在")


class CheckpointExists(Exception):
    """checkpoint_id 已被另一播放存档占用（对应 HTTP 409）。"""

    def __init__(self, checkpoint_id: str) -> None:
        self.checkpoint_id = checkpoint_id
        super().__init__(
            f"播放存档 {checkpoint_id!r} 已存在，存档必须使用新的 checkpoint_id"
        )


class CheckpointMissing(Exception):
    """checkpoint_id 不存在（对应 HTTP 404）。"""

    def __init__(self, checkpoint_id: str) -> None:
        self.checkpoint_id = checkpoint_id
        super().__init__(f"播放存档 {checkpoint_id!r} 不存在")


@dataclass
class CheckpointRecord:
    """一次播放存档：存档 id、源会话 id 与完整播放状态负载（JSON 反序列化结果）。"""

    checkpoint_id: str
    session_id: str
    payload: Dict[str, Any]


@dataclass
class SceneRecord:
    scene_id: str
    revision: int
    track_count: int
    subtitles: List[Dict[str, Any]]
    assignments: List[Dict[str, Any]]
    changes: int


class SceneStore:
    """SQLite 场景存储。线程安全：每次操作使用独立连接（check_same_thread=False）。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL 允许读写并发；busy_timeout 在写锁竞争时等待而非立即报错
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scenes (
                    scene_id      TEXT PRIMARY KEY,
                    revision      INTEGER NOT NULL,
                    track_count   INTEGER NOT NULL,
                    subtitles     TEXT    NOT NULL,
                    assignments   TEXT    NOT NULL,
                    changes       INTEGER NOT NULL,
                    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
                """
            )
            # 播放存档（跨重启续播）：payload 为会话完整播放状态的 JSON。
            # 存档与场景使用相互独立的 id 空间（不同表）；同名存档插入冲突。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS playback_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    session_id    TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
                """
            )

    def commit_revision(
        self,
        scene_id: str,
        expected_revision: int,
        track_count: int,
        subtitles: List[Dict[str, Any]],
        assignments: List[Dict[str, Any]],
        changes: int,
    ) -> int:
        """以乐观锁写入一次成功修订，返回新版本号。

        新场景（expected_revision == 0 且库中不存在）写入 revision=1；
        已存在场景仅当 expected_revision == 当前 revision 时覆盖为 +1；
        否则抛出 RevisionConflict，且不改动任何已有数据。
        """
        conn = self._connect()
        try:
            # IMMEDIATE 立即获取保留写锁，串行化并发提交：
            # 同一 expected_revision 的并发提交只有第一个能通过版本核对。
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM scenes WHERE scene_id = ?", (scene_id,)
            ).fetchone()

            if row is None:
                if expected_revision != 0:
                    raise RevisionConflict(scene_id, 0)
                new_revision = 1
                conn.execute(
                    """
                    INSERT INTO scenes
                        (scene_id, revision, track_count, subtitles, assignments, changes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scene_id,
                        new_revision,
                        track_count,
                        json.dumps(subtitles, ensure_ascii=False),
                        json.dumps(assignments, ensure_ascii=False),
                        changes,
                    ),
                )
            else:
                current_revision = row["revision"]
                if expected_revision != current_revision:
                    raise RevisionConflict(scene_id, current_revision)
                new_revision = current_revision + 1
                conn.execute(
                    """
                    UPDATE scenes
                       SET revision = ?, track_count = ?, subtitles = ?,
                           assignments = ?, changes = ?,
                           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                     WHERE scene_id = ?
                    """,
                    (
                        new_revision,
                        track_count,
                        json.dumps(subtitles, ensure_ascii=False),
                        json.dumps(assignments, ensure_ascii=False),
                        changes,
                        scene_id,
                    ),
                )
            conn.commit()
            return new_revision
        except RevisionConflict:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def commit_clip(
        self,
        source_scene_id: str,
        target_scene_id: str,
        expected_revision: int,
        track_count: int,
        subtitles: List[Dict[str, Any]],
        assignments: List[Dict[str, Any]],
        changes: int,
    ) -> SceneRecord:
        """在单个 BEGIN IMMEDIATE 事务内核对源版本、占用目标并写入片段。

        片段始终以 revision=1 作为全新场景写入。事务内依次完成：
        1. 源场景必须存在且 revision == expected_revision；
        2. target_scene_id 必须尚未被任何场景占用（包括源/目标同名）；
        3. INSERT 目标片段。

        任一条件不满足都在提交前抛错回滚：失败不会留下目标，也不会改动
        源场景。源与目标的版本核对被同一把写锁串行化，并发裁剪/修订不会
        在“读到源、提交目标”之间被插入过期窗口。

        这是提交前的第二道版本核对：调用方（路由）在读取源快照时已先核对
        过一次，保证裁剪所用数据取自与 expected_revision 一致的完整快照；
        此处再核对覆盖“读取匹配后、提交前源被修订”的窗口。
        返回已提交的目标场景记录（revision=1）。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            source_row = conn.execute(
                "SELECT revision FROM scenes WHERE scene_id = ?",
                (source_scene_id,),
            ).fetchone()
            if source_row is None:
                raise SourceSceneMissing(source_scene_id)
            current_revision = source_row["revision"]
            if expected_revision != current_revision:
                raise RevisionConflict(source_scene_id, current_revision)

            target_row = conn.execute(
                "SELECT revision FROM scenes WHERE scene_id = ?",
                (target_scene_id,),
            ).fetchone()
            if target_row is not None:
                raise TargetSceneExists(
                    target_scene_id, target_row["revision"]
                )

            conn.execute(
                """
                INSERT INTO scenes
                    (scene_id, revision, track_count, subtitles, assignments, changes)
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (
                    target_scene_id,
                    track_count,
                    json.dumps(subtitles, ensure_ascii=False),
                    json.dumps(assignments, ensure_ascii=False),
                    changes,
                ),
            )
            conn.commit()
            return SceneRecord(
                scene_id=target_scene_id,
                revision=1,
                track_count=track_count,
                subtitles=[dict(s) for s in subtitles],
                assignments=[dict(a) for a in assignments],
                changes=changes,
            )
        except (RevisionConflict, TargetSceneExists, SourceSceneMissing):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def commit_concat(
        self,
        target_scene_id: str,
        sources: List[Tuple[str, int]],
        track_count: int,
        subtitles: List[Dict[str, Any]],
        assignments: List[Dict[str, Any]],
        changes: int,
    ) -> SceneRecord:
        """在单个 BEGIN IMMEDIATE 事务内核对全部源版本、占用目标并写入拼接场景。

        sources 为按请求段序列排列的 (scene_id, expected_revision) 列表，
        同源可以重复。拼接结果始终以 revision=1 作为全新场景写入。
        事务内依次完成：
        1. 每个源场景都必须存在且 revision == 该段的 expected_revision；
        2. target_scene_id 必须尚未被任何场景占用（包括与某个源同名）；
        3. INSERT 目标拼接场景。

        任一条件不满足都在提交前抛错回滚：失败不会留下目标，也不会改动
        任何源场景。全部源的版本核对与目标创建被同一把写锁串行化并一同
        生效，并发修订/拼接不会在“读到源、提交目标”之间插入过期窗口。

        这是提交前的第二道版本核对：调用方（路由）在读取各源快照时已按
        段序核对过一次，保证拼接所用数据全部取自与 expected_revision 一致
        的完整快照；此处再核对覆盖“读取匹配后、提交前任一源被修订”的
        窗口。返回已提交的目标场景记录（revision=1）。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 按段序列逐个核对：404/409 的 detail 能指出具体是哪一段
            for source_scene_id, expected_revision in sources:
                source_row = conn.execute(
                    "SELECT revision FROM scenes WHERE scene_id = ?",
                    (source_scene_id,),
                ).fetchone()
                if source_row is None:
                    raise SourceSceneMissing(source_scene_id)
                current_revision = source_row["revision"]
                if expected_revision != current_revision:
                    raise RevisionConflict(source_scene_id, current_revision)

            target_row = conn.execute(
                "SELECT revision FROM scenes WHERE scene_id = ?",
                (target_scene_id,),
            ).fetchone()
            if target_row is not None:
                raise TargetSceneExists(
                    target_scene_id, target_row["revision"]
                )

            conn.execute(
                """
                INSERT INTO scenes
                    (scene_id, revision, track_count, subtitles, assignments, changes)
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (
                    target_scene_id,
                    track_count,
                    json.dumps(subtitles, ensure_ascii=False),
                    json.dumps(assignments, ensure_ascii=False),
                    changes,
                ),
            )
            conn.commit()
            return SceneRecord(
                scene_id=target_scene_id,
                revision=1,
                track_count=track_count,
                subtitles=[dict(s) for s in subtitles],
                assignments=[dict(a) for a in assignments],
                changes=changes,
            )
        except (RevisionConflict, TargetSceneExists, SourceSceneMissing):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_scene(self, scene_id: str) -> Optional[SceneRecord]:
        """按 scene_id 读取最近一次成功修订；不存在返回 None。

        单行单行快照读取（WAL 下一条 SELECT 对同一行取一致版本），
        不会读到某次提交的部分字段。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT scene_id, revision, track_count, subtitles, assignments, changes
                  FROM scenes WHERE scene_id = ?
                """,
                (scene_id,),
            ).fetchone()
        if row is None:
            return None
        subtitles, assignments = _loads_pair(row["subtitles"], row["assignments"])
        return SceneRecord(
            scene_id=row["scene_id"],
            revision=row["revision"],
            track_count=row["track_count"],
            subtitles=subtitles,
            assignments=assignments,
            changes=row["changes"],
        )

    def save_checkpoint(
        self,
        checkpoint_id: str,
        session_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """在单个 BEGIN IMMEDIATE 事务内占用 checkpoint_id 并写入播放存档。

        存档一经写入即不可变（不提供更新/删除）：同名 id 的并发或重复
        写入在事务内发现主键已占用即抛 CheckpointExists 并回滚，失败不留
        存档、也不影响先成功写入的那份。payload 为调用方在会话锁内序列化
        的完整播放状态（快照、游标、隐藏集合、通道队列与序号边界、自动
        播放状态）。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM playback_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
            if existing is not None:
                raise CheckpointExists(checkpoint_id)
            conn.execute(
                """
                INSERT INTO playback_checkpoints
                    (checkpoint_id, session_id, payload)
                VALUES (?, ?, ?)
                """,
                (
                    checkpoint_id,
                    session_id,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            conn.commit()
        except CheckpointExists:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_checkpoint(self, checkpoint_id: str) -> Optional[CheckpointRecord]:
        """按 checkpoint_id 读取播放存档；不存在返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT checkpoint_id, session_id, payload
                  FROM playback_checkpoints WHERE checkpoint_id = ?
                """,
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return CheckpointRecord(
            checkpoint_id=row["checkpoint_id"],
            session_id=row["session_id"],
            payload=json.loads(row["payload"]),
        )


def _loads_pair(subtitles_json: str, assignments_json: str) -> Tuple[List, List]:
    """同源反序列化，保证读取到的字幕与分轨属于同一次提交。"""
    subtitles: List[Dict[str, Any]] = json.loads(subtitles_json)
    assignments: List[Dict[str, Any]] = json.loads(assignments_json)
    return subtitles, assignments
