"""FastAPI 入口：请求/响应模型与路由。"""
from __future__ import annotations

from threading import Lock
from typing import List, Literal, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from . import storage as storage_module
from .playback import (
    AcceptConflict,
    AckOutOfRange,
    AutoplayConflict,
    ChannelCapacityExhausted,
    ChannelExists,
    ChannelMissing,
    CheckpointNotAllowed,
    CursorRegression,
    PlaybackManager,
    PlaybackSession,
    SceneUnavailable,
    SessionFinished,
    SessionMissing,
)
from .solver import Subtitle, solve, validate
from .storage import (
    CheckpointExists,
    RevisionConflict,
    SceneRecord,
    SceneStore,
    SourceSceneMissing,
    TargetSceneExists,
)

app = FastAPI(
    title="对白字幕分轨 API",
    description=(
        "为离线对白字幕分配屏幕轨道：时间重叠的字幕不同轨，"
        "同一说话人按时间相邻字幕的换轨次数最少，并列取轨道号序列字典序最小者。"
        "场景（scene）可按 scene_id 保存修订快照，并以版本号做乐观并发控制；"
        "已保存场景的指定时段可另存为从 0 毫秒起、保持原画面轨道的独立片段；"
        "2～4 个已保存场景还可按给定顺序拼接为连续播放场景：时间平移后按段序、"
        "源顺序排列，id 为段号:段内序号，源分轨写入 locked_track，读取与提交时"
        "均核对全部源版本，提交核对与目标创建一同生效，成功保存为版本 1。"
        "播放会话（session）按 scene_id 创建内存态会话，游标推进中可接纳场景"
        "后续修订：已开始字幕保持原分轨，其余按原规则重算。事件推进接口返回"
        "一次推进跨过的全部字幕进入/退出事件，短字幕不会在两次采样之间漏掉。"
        "播放时可按 speaker 精确匹配临时隐藏指定说话人的字幕（仅影响该会话、"
        "不持久化），切换时按当前游标的活动区间返回 exit/enter 显示变更事件，"
        "隐藏后推进与事件推进的活动字幕及事件均排除隐藏者，游标照常推进。"
        "播放结束可通过 finish 一次取回最终完整快照并释放会话：结束与推进、"
        "事件推进、接纳、隐藏切换串行生效，结束后该会话 id 的一切请求均返回 404。"
        "还可为会话开启补取通道：普通推进、事件推进与隐藏切换产生的全部 "
        "enter/exit 按事件字段与排序入队（seq 从 1 连续编号），推进响应丢失后"
        "提交 ack_seq 补取未确认事件——只能确认此前补取已返回的最大 seq 以内的"
        "前缀，旧确认不回退；通道仅内存态，结束会话即清除，不跨重启恢复。"
        "通道共享容量由 CHANNEL_EVENT_LIMIT 配置（正整数，默认 1024，非法配置"
        "拒绝启动，可经 Compose 配置）：它是单进程内所有会话未确认事件总量的"
        "上限；普通推进、事件推进与隐藏切换按本次实际事件数原子申请容量，与"
        "状态变更、完整入队一同生效，不足立即 503（不推进游标、不改隐藏集合、"
        "不消费 seq、不部分入队）；未开启通道不占容量，无事件操作满额仍可执行。"
        "容量仅在确认并删除前缀或结束会话时释放（重复/并发结束不重复释放），"
        "释放后原请求可重试。"
        "已开启补取通道且从未启动的会话还可启动自动播放：进程内后台驱动每 "
        "50 毫秒按单调时钟经过的整毫秒自动推进游标（暂停、阻塞不计时，恢复 "
        "从保留游标重新计时、不追赶停顿时间；启动/恢复的计时段起点在会话锁内、"
        "控制请求串行生效的同一时刻读取，等锁时间不计入），沿用事件窗口、隐藏"
        "过滤、排序、"
        "seq 与共享容量的全部规则——整批事件容量不足即转为阻塞，游标、seq 与"
        "队列不变，确认释放后须显式恢复；可经 HTTP 启动、暂停、恢复、查询"
        "（idle/running/paused/blocked），状态不符 409、未知会话 404，运行中"
        "的两种手动推进 409，其他既有会话操作仍可用并完整串行；驱动状态仅存"
        "单进程内存，旧接口响应不变。"
        "已开补取通道且自动播放未运行的会话还可按 session_id 与非空 "
        "checkpoint_id 保存 SQLite 播放存档：存档含完整快照、游标、隐藏集合、"
        "通道未确认事件与 next_seq/acked_seq/max_returned_seq 序号边界，与同"
        "会话操作串行，成功不结束会话、失败不留存档；未开通道或自动播放运行"
        "中返回 409，同名存档 409，未知会话 404，非法 id 422。按存档可恢复"
        "为全新 session_id 的独立会话（不读取当前场景）：队列计入共享容量，"
        "不足 503 且不留会话或容量占用，并发不超额；事件、序号与确认边界保留，"
        "idle 仍为 idle、paused/blocked 恢复为 paused，续播重新计时、不追赶"
        "离线时间；存档可重复恢复，后续操作不改存档或其他会话。"
    ),
    version="1.11.0",
)


class SubtitleIn(BaseModel):
    id: Union[str, int] = Field(..., description="全局唯一的字幕 id")
    speaker: str = Field(..., description="说话人标识，非空")
    start_ms: int = Field(..., ge=0, description="开始时间（毫秒，非负）")
    end_ms: int = Field(..., ge=0, description="结束时间（毫秒，必须大于 start_ms）")
    locked_track: Optional[int] = Field(
        default=None, ge=1, description="可选：锁定轨道号（从 1 开始）"
    )

    @field_validator("speaker")
    @classmethod
    def _speaker_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("speaker 不能为空")
        return v

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, v: Union[str, int]) -> Union[str, int]:
        if isinstance(v, str) and v == "":
            raise ValueError("id 不能为空字符串")
        return v

    @model_validator(mode="after")
    def _check_interval(self) -> "SubtitleIn":
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms 必须小于 end_ms")
        return self


class AssignRequest(BaseModel):
    track_count: int = Field(
        ..., ge=2, le=3, description="屏幕轨道数（2～3，轨道号从 1 开始）"
    )
    subtitles: List[SubtitleIn] = Field(
        ..., min_length=1, max_length=12, description="字幕列表（1～12 条，按输入顺序）"
    )

    @model_validator(mode="after")
    def _check_cross_item_rules(self) -> "AssignRequest":
        subs = [
            Subtitle(
                id=s.id,
                speaker=s.speaker,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                locked_track=s.locked_track,
            )
            for s in self.subtitles
        ]
        error = validate(subs, self.track_count)
        if error is not None:
            raise ValueError(error)
        return self


class SceneRevisionRequest(AssignRequest):
    """场景修订请求：沿用 /assign 的全部字段与校验，另带版本号。

    expected_revision：客户端所依据的当前版本。新场景填 0；
    已存在场景必须填写当前版本，成功后版本 +1。
    """

    expected_revision: int = Field(
        ..., ge=0, description="所依据的当前场景版本；新场景填 0"
    )


class ClipRequest(BaseModel):
    """片段另存请求：把已保存场景的 [from_ms, to_ms) 另存为新场景。

    时间字段使用严格整数（strict）：布尔值（true/false 会被 Python 视为
    int 子类）与数字字符串一律按 422 拒绝，不做隐式转换。
    """

    target_scene_id: str = Field(
        ..., description="片段的新场景 id：非空且尚未被占用"
    )
    expected_revision: int = Field(
        ..., ge=0, description="所依据的源场景当前版本"
    )
    from_ms: int = Field(
        ...,
        ge=0,
        strict=True,
        description="裁剪起点（毫秒，非负整数，拒绝布尔值与字符串）",
    )
    to_ms: int = Field(
        ...,
        ge=0,
        strict=True,
        description="裁剪终点（毫秒，非负整数且严格大于 from_ms）",
    )

    @field_validator("target_scene_id")
    @classmethod
    def _target_scene_id_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("target_scene_id 不能为空")
        return v

    @model_validator(mode="after")
    def _check_interval(self) -> "ClipRequest":
        if self.from_ms >= self.to_ms:
            raise ValueError("from_ms 必须小于 to_ms")
        return self


class ConcatSegmentIn(BaseModel):
    """拼接请求中的一段：引用一个已保存场景及其版本与占用时长。

    整数字段使用严格整数（strict）：布尔值（true/false 会被 Python 视为
    int 子类）与数字字符串一律按 422 拒绝，不做隐式转换。
    """

    scene_id: str = Field(..., description="本段引用的已保存场景 id：非空")
    expected_revision: int = Field(
        ...,
        gt=0,
        strict=True,
        description="所依据的源场景当前版本（正整数，拒绝布尔值与字符串）",
    )
    duration_ms: int = Field(
        ...,
        gt=0,
        strict=True,
        description=(
            "本段在连续播放时间轴上占用的时长（毫秒，正整数），不得小于源场景"
            "字幕的最大 end_ms"
        ),
    )

    @field_validator("scene_id")
    @classmethod
    def _scene_id_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("scene_id 不能为空")
        return v


class ConcatRequest(BaseModel):
    """场景拼接请求：按给定顺序把 2～4 个已保存场景拼成连续播放场景。

    各源场景可重复引用；目标 id 必须非空且尚未被占用。跨源约束
    （轨道数一致、各段时长不小于该源最大 end_ms、字幕总数 ≤ 12）
    在路由中读取源快照后核对，非法返回 422。
    """

    target_scene_id: str = Field(
        ..., description="拼接结果的新场景 id：非空且尚未被占用"
    )
    segments: List[ConcatSegmentIn] = Field(
        ..., min_length=2, max_length=4, description="按给定顺序排列的 2～4 段"
    )

    @field_validator("target_scene_id")
    @classmethod
    def _target_scene_id_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("target_scene_id 不能为空")
        return v


class TrackAssignment(BaseModel):
    id: Union[str, int]
    track: int


class AssignResponse(BaseModel):
    feasible: bool
    changes: int = Field(..., description="同一说话人按时间相邻字幕的换轨总数")
    assignments: List[TrackAssignment]


class SubtitleOut(BaseModel):
    id: Union[str, int]
    speaker: str
    start_ms: int
    end_ms: int
    locked_track: Optional[int] = None


class SceneResponse(BaseModel):
    """一次成功修订保存后的完整场景快照。"""

    scene_id: str
    revision: int = Field(..., description="本次提交成功后的新版本号")
    track_count: int
    subtitles: List[SubtitleOut]
    changes: int
    assignments: List[TrackAssignment]
    feasible: bool = True


# ---------------------------------------------------------------------------
# 播放会话：请求/响应模型
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    scene_id: str = Field(..., description="要播放的场景 id")


class SessionStateResponse(BaseModel):
    """会话完整状态：创建与接纳成功后返回。"""

    session_id: str
    scene_id: str
    source_revision: int = Field(..., description="会话快照来源的场景版本")
    cursor_ms: int = Field(..., description="播放游标（毫秒）")
    track_count: int
    changes: int
    subtitles: List[SubtitleOut]
    assignments: List[TrackAssignment]


class ActiveSubtitleOut(BaseModel):
    id: Union[str, int]
    speaker: str
    start_ms: int
    end_ms: int
    locked_track: Optional[int] = None
    track: int


class AdvanceRequest(BaseModel):
    time_ms: int = Field(
        ..., ge=0, description="推进到的时刻（毫秒，非负整数，只能不减）"
    )


class AdvanceResponse(BaseModel):
    """推进结果：源版本、游标，以及游标时刻 [start_ms, end_ms) 内的字幕和轨道。"""

    session_id: str
    scene_id: str
    source_revision: int
    cursor_ms: int
    active: List[ActiveSubtitleOut]


class SubtitleEventOut(BaseModel):
    """一次推进跨过的单条字幕事件：start_ms 进入，end_ms 退出。"""

    id: Union[str, int]
    speaker: str
    track: int
    time_ms: int = Field(..., description="事件时刻（毫秒）：进入取 start_ms，退出取 end_ms")
    type: Literal["enter", "exit"] = Field(
        ..., description="进入（enter）或退出（exit）"
    )


class AdvanceEventsResponse(BaseModel):
    """事件推进结果：本次跨过的事件、推进后的活动字幕、源版本与游标。

    事件窗口为（旧游标, time_ms]，按时刻升序，同刻先退出后进入，
    同类按会话字幕顺序；等时推进事件为空。
    """

    session_id: str
    scene_id: str
    source_revision: int
    cursor_ms: int
    events: List[SubtitleEventOut]
    active: List[ActiveSubtitleOut]


class HiddenSpeakersRequest(BaseModel):
    """临时隐藏说话人设置：按 speaker 精确匹配的无重复非空字符串数组。

    空数组清除全部隐藏；元素允许是会话字幕中尚未出现（甚至永不出现）
    的说话人。非数组、缺失或 null、元素不是字符串（数字/布尔等不做
    隐式转换）、空字符串、重复元素一律按 422 拒绝。
    """

    hidden_speakers: List[str] = Field(
        ..., description="要隐藏的说话人集合：无重复非空字符串数组，空数组清除"
    )

    @field_validator("hidden_speakers", mode="after")
    @classmethod
    def _no_empty_or_duplicate(cls, v: List[str]) -> List[str]:
        for speaker in v:
            if speaker == "":
                raise ValueError("hidden_speakers 不能包含空字符串")
        if len(set(v)) != len(v):
            raise ValueError("hidden_speakers 不能包含重复的说话人")
        return v


class HiddenSpeakersResponse(BaseModel):
    """隐藏切换结果：集合、游标、源版本、切换后可见活动字幕与显示变更事件。

    游标不移动：events 为当前游标活动区间内的显示变化——原可见而现隐藏者
    发 exit，原隐藏而现可见者发 enter，事件时刻为当前游标；先 exit 后
    enter，同类按会话字幕顺序；集合无变化时 events 为空。
    """

    session_id: str
    scene_id: str
    source_revision: int
    cursor_ms: int
    hidden_speakers: List[str]
    events: List[SubtitleEventOut]
    active: List[ActiveSubtitleOut]


class ChannelOpenResponse(BaseModel):
    """补取通道开启结果：当前可见活动字幕与起始序号 0。

    通道自此记录普通推进、事件推进与隐藏切换产生的全部 enter/exit，
    首个事件序号为 1；开启之前的事件不补记，客户端以本响应的 active
    作为初始可见集合。
    """

    session_id: str
    scene_id: str
    source_revision: int
    cursor_ms: int
    seq: int = Field(
        ..., description="通道当前序号：开启时为 0，事件从 1 起连续编号"
    )
    active: List[ActiveSubtitleOut]


class ChannelFetchRequest(BaseModel):
    """补取请求：确认已收到的事件前缀。

    ack_seq 使用严格整数（strict）：布尔值（true/false 会被 Python 视为
    int 子类）与数字字符串一律按 422 拒绝，不做隐式转换。
    """

    ack_seq: int = Field(
        ...,
        ge=0,
        strict=True,
        description="确认位置（非负整数，拒绝布尔值与字符串）",
    )


class ChannelEventOut(BaseModel):
    """通道中的一条已排队事件：连续序号加事件推进的事件字段。"""

    seq: int = Field(..., description="事件序号（从 1 起连续编号）")
    id: Union[str, int]
    speaker: str
    track: int
    time_ms: int = Field(..., description="事件时刻（毫秒）")
    type: Literal["enter", "exit"] = Field(
        ..., description="进入（enter）或退出（exit）"
    )


class ChannelFetchResponse(BaseModel):
    """补取结果：确认位置与剩余未确认事件（按 seq 递增）。

    未确认事件重复补取时内容与序号不变；已确认前缀随即丢弃。
    """

    session_id: str
    scene_id: str
    acked_seq: int = Field(
        ..., description="确认位置：已确认的最大事件序号（只增不回退）"
    )
    events: List[ChannelEventOut]


class AutoplayStateResponse(BaseModel):
    """自动播放控制结果：状态与当前游标。

    status 为 idle（未启动）/running（运行中）/paused（已暂停）/
    blocked（容量满额阻塞）之一；cursor_ms 为后台最近一次成功推进保留的
    游标（暂停、阻塞不计时，恢复后从该游标重新计时）。
    """

    session_id: str
    status: Literal["idle", "running", "paused", "blocked"] = Field(
        ..., description="自动播放状态：未启动/运行/暂停/阻塞"
    )
    cursor_ms: int = Field(..., description="当前播放游标（毫秒）")


class CheckpointSaveResponse(BaseModel):
    """播放存档保存结果：存档/会话 id、播放状态与存档时的通道序号边界。

    成功存档不结束会话：原会话、游标、隐藏集合与通道队列完全不变，
    可继续推进或用其他 checkpoint_id 再次存档。autoplay_status 与 status
    同义（存档只允许 idle/paused/blocked，运行中返回 409）。
    """

    checkpoint_id: str
    session_id: str
    scene_id: str
    source_revision: int
    cursor_ms: int
    status: Literal["idle", "paused", "blocked"] = Field(
        ..., description="存档时的自动播放状态（运行中不可存档）"
    )
    autoplay_status: Literal["idle", "paused", "blocked"] = Field(
        ..., description="status 的同义字段（与自动播放接口命名一致）"
    )
    next_seq: int = Field(
        ..., description="存档通道的下一事件序号（恢复后新事件从此编号）"
    )
    acked_seq: int = Field(..., description="存档时已确认的最大事件序号")
    max_returned_seq: int = Field(
        ..., description="存档时补取曾返回过的最大事件序号"
    )
    queued_events: int = Field(
        ..., description="随存档保留的未确认通道事件数（恢复时计入共享容量）"
    )


class CheckpointChannelSnapshot(BaseModel):
    """恢复后补取通道的确认边界、序号与保留的未确认事件。"""

    acked_seq: int
    next_seq: int
    max_returned_seq: int
    queued_count: int = Field(
        ..., description="恢复出的未确认事件数（已计入共享容量）"
    )
    events: List[ChannelEventOut]


class CheckpointRestoreResponse(BaseModel):
    """按存档恢复的结果：新会话 id、完整快照与播放状态。

    新会话完全独立：session_id 为全新值，快照只取自存档（不读取当前
    场景）；status 为 idle（存档时未启动）或 paused（存档时暂停/阻塞），
    续播需显式 resume 且从保留游标重新计时、不追赶离线时间。
    hidden_speakers 为存档隐藏集合（排序回显）；channel 给出恢复后的
    确认边界、序号与保留的未确认事件，后续补取与新事件接着 next_seq
    连续编号。
    """

    session_id: str = Field(..., description="恢复产生的全新播放会话 id")
    checkpoint_id: str
    source_session_id: str = Field(
        ..., description="存档来源的原会话 id（仅信息用途）"
    )
    scene_id: str
    source_revision: int
    cursor_ms: int
    status: Literal["idle", "paused"] = Field(
        ..., description="恢复后的自动播放状态（未启动/暂停）"
    )
    autoplay_status: Literal["idle", "paused"] = Field(
        ..., description="status 的同义字段（与自动播放接口命名一致）"
    )
    track_count: int
    changes: int
    hidden_speakers: List[str]
    subtitles: List[SubtitleOut]
    assignments: List[TrackAssignment]
    channel: CheckpointChannelSnapshot


# ---------------------------------------------------------------------------
# SQLite 存储：惰性单例，路径可用环境变量 SCENES_DB_PATH 覆盖
# ---------------------------------------------------------------------------

_store: Optional[SceneStore] = None
_store_lock = Lock()


def get_store() -> SceneStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SceneStore(storage_module.DEFAULT_DB_PATH)
    return _store


def reset_store() -> None:
    """重置单例（供测试重新指定 SCENES_DB_PATH 后使用）。"""
    global _store
    with _store_lock:
        _store = None


# ---------------------------------------------------------------------------
# 播放会话管理器：单进程内存，不持久化、不跨重启保留
# ---------------------------------------------------------------------------

_playback: Optional[PlaybackManager] = None
_playback_lock = Lock()


def get_playback() -> PlaybackManager:
    global _playback
    if _playback is None:
        with _playback_lock:
            if _playback is None:
                _playback = PlaybackManager()
    return _playback


def reset_playback() -> None:
    """清空全部播放会话（供测试隔离；会话本就不跨重启保留）。"""
    global _playback
    with _playback_lock:
        if _playback is not None:
            # 退役管理器时停止其自动播放后台驱动，避免旧线程跨管理器存活。
            _playback.shutdown_driver()
        _playback = None


def _subtitles_from_request(req: AssignRequest) -> List[Subtitle]:
    return [
        Subtitle(
            id=s.id,
            speaker=s.speaker,
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            locked_track=s.locked_track,
        )
        for s in req.subtitles
    ]


def _check_scene_id(scene_id: str) -> None:
    if scene_id.strip() == "":
        raise HTTPException(status_code=422, detail="scene_id 不能为空")


class EmptyClip(Exception):
    """裁剪窗口与源场景没有任何相交字幕（对应 HTTP 422）。"""


def _build_clip(
    record: SceneRecord, from_ms: int, to_ms: int
) -> tuple[List[Subtitle], List[int], int]:
    """构造 [from_ms, to_ms) 片段：保留相交字幕，保持源画面轨道，时间归零。

    - 仅保留与 [from_ms, to_ms) 左闭右开区间相交的字幕（源顺序不变）；
    - 起止时间裁到窗口内再整体减去 from_ms，片段时间轴从 0 毫秒起；
    - id、speaker 沿用源字幕；源场景为该 id 求出的分轨写入 locked_track，
      再以源 track_count 按既有最少换轨、并列规则重算——由于源分轨互相
      不冲突且锁轨与源分轨一致，求解结果必然等于源画面轨道；
    - 窗口内没有相交字幕时抛 EmptyClip。
    """
    track_by_id = {a["id"]: a["track"] for a in record.assignments}
    subs: List[Subtitle] = []
    for s in record.subtitles:
        start_ms, end_ms = s["start_ms"], s["end_ms"]
        # 左闭右开：start < to 且 end > from 才相交
        if start_ms >= to_ms or end_ms <= from_ms:
            continue
        subs.append(
            Subtitle(
                id=s["id"],
                speaker=s["speaker"],
                start_ms=max(start_ms, from_ms) - from_ms,
                end_ms=min(end_ms, to_ms) - from_ms,
                locked_track=track_by_id[s["id"]],
            )
        )
    if not subs:
        raise EmptyClip()
    tracks, changes = solve(subs, record.track_count)
    if tracks is None:  # 理论不可达：源分轨本身即是合法解
        raise RuntimeError("裁剪片段在固定源分轨后无解")
    return subs, tracks, changes


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/assign", response_model=AssignResponse)
def assign(req: AssignRequest) -> AssignResponse:
    subs = _subtitles_from_request(req)
    tracks, changes = solve(subs, req.track_count)
    if tracks is None:
        # 锁定冲突 / 轨道不足：无解，方案为空（HTTP 仍为 200；非法输入才是 422）
        return AssignResponse(feasible=False, changes=0, assignments=[])
    return AssignResponse(
        feasible=True,
        changes=changes,
        assignments=[
            TrackAssignment(id=s.id, track=t) for s, t in zip(subs, tracks)
        ],
    )


@app.put(
    "/scenes/{scene_id}",
    response_model=Union[SceneResponse, AssignResponse],
)
def revise_scene(
    scene_id: str, req: SceneRevisionRequest
) -> Union[SceneResponse, AssignResponse]:
    """创建或修订场景。

    - 新场景：expected_revision=0，成功后 revision=1；
    - 修订：expected_revision 必须等于当前版本，成功后版本 +1；
    - 合法但无解：沿用 /assign 的无解响应（feasible=false），不保存、不改版本；
    - 非法输入：422，不改变已有版本；
    - 版本不符（含同一版本的并发竞争落败方）：409，不改变已有版本。
    """
    _check_scene_id(scene_id)

    # 校验由 Pydantic 模型在进入本函数前完成（非法 -> 422）。
    # 求解沿用 /assign 的同一套校验、最少换轨与并列规则。
    subs = _subtitles_from_request(req)
    tracks, changes = solve(subs, req.track_count)
    if tracks is None:
        # 合法但无解：与 /assign 完全相同的响应，且不落库
        return AssignResponse(feasible=False, changes=0, assignments=[])

    subtitles_data = [s.model_dump() for s in req.subtitles]
    assignments_data = [
        {"id": s.id, "track": t} for s, t in zip(subs, tracks)
    ]

    try:
        new_revision = get_store().commit_revision(
            scene_id=scene_id,
            expected_revision=req.expected_revision,
            track_count=req.track_count,
            subtitles=subtitles_data,
            assignments=assignments_data,
            changes=changes,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )

    return SceneResponse(
        scene_id=scene_id,
        revision=new_revision,
        track_count=req.track_count,
        subtitles=[s.model_dump() for s in req.subtitles],
        changes=changes,
        assignments=[
            TrackAssignment(id=a["id"], track=a["track"]) for a in assignments_data
        ],
    )


def _record_to_scene_response(record: SceneRecord) -> SceneResponse:
    return SceneResponse(
        scene_id=record.scene_id,
        revision=record.revision,
        track_count=record.track_count,
        subtitles=record.subtitles,
        changes=record.changes,
        assignments=record.assignments,
    )


@app.get("/scenes/{scene_id}", response_model=SceneResponse)
def read_scene(scene_id: str) -> SceneResponse:
    """读取场景最近一次成功修订的完整快照；不存在返回 404。"""
    _check_scene_id(scene_id)
    record = get_store().get_scene(scene_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"场景 {scene_id!r} 不存在")
    return _record_to_scene_response(record)


@app.post("/scenes/{scene_id}/clip", response_model=SceneResponse)
def clip_scene(scene_id: str, req: ClipRequest) -> SceneResponse:
    """把已保存场景的 [from_ms, to_ms) 另存为从 0 毫秒起的独立片段。

    保留与窗口相交（左闭右开）的字幕，源顺序不变；起止时间裁到窗口内
    再减去 from_ms；id、speaker 沿用源字幕，源分轨写入各条 locked_track，
    目标沿用源 track_count 与既有求解规则，因此片段保持源画面轨道。
    片段保存为 revision=1 的全新场景，返回其完整快照。

    - 源场景不存在：404；
    - from_ms >= to_ms、时间为布尔/字符串、target_scene_id 为空等
      非法请求：422（Pydantic 校验）；窗口内无相交字幕：422；
    - expected_revision 过期或超前、target_scene_id 已被占用（含与源同名）：409。

    版本核对分两道：读取源快照时先核对 record.revision ==
    expected_revision——裁剪所用的 track_count、字幕与源分轨全部取自
    这份快照，读取时版本不符立即 409，绝不拿错配快照裁剪；提交前在
    BEGIN IMMEDIATE 事务内再核对一次，覆盖“读取匹配后、提交前源被
    修订”的窗口。任一道失败既不会留下目标场景，也不会改动源场景或
    任何播放会话（播放会话本就是不被场景读取所修改的内存快照）。
    目标创建后可直接用既有读取、修订与播放接口操作。
    """
    _check_scene_id(scene_id)

    record = get_store().get_scene(scene_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"场景 {scene_id!r} 不存在"
        )
    # 读取即核对：track_count、字幕与分轨都来自 record，它的版本必须与
    # expected_revision 完全一致（过期或超前都算冲突）。current_revision
    # 如实报告“本次读到的版本”，客户端可据此重新 GET 后重试。
    if record.revision != req.expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": scene_id,
                "current_revision": record.revision,
            },
        )

    try:
        subs, tracks, changes = _build_clip(
            record, req.from_ms, req.to_ms
        )
    except EmptyClip:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "裁剪窗口 [from_ms, to_ms) 内没有与任何字幕相交的时段",
                "scene_id": scene_id,
                "from_ms": req.from_ms,
                "to_ms": req.to_ms,
            },
        )

    subtitles_data = [
        {
            "id": s.id,
            "speaker": s.speaker,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "locked_track": s.locked_track,
        }
        for s in subs
    ]
    assignments_data = [
        {"id": s.id, "track": t} for s, t in zip(subs, tracks)
    ]

    try:
        target_record = get_store().commit_clip(
            source_scene_id=scene_id,
            target_scene_id=req.target_scene_id,
            expected_revision=req.expected_revision,
            track_count=record.track_count,
            subtitles=subtitles_data,
            assignments=assignments_data,
            changes=changes,
        )
    except SourceSceneMissing:
        # 源在读取后、事务内被删除（正常流程中场景不可删除）：按 404 处理
        raise HTTPException(
            status_code=404, detail=f"场景 {scene_id!r} 不存在"
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )
    except TargetSceneExists as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "target_scene_id 已被占用，裁剪目标必须使用新的 scene_id",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )

    return _record_to_scene_response(target_record)


def _build_concat(
    records: List[SceneRecord], durations: List[int]
) -> tuple[List[Subtitle], List[int], int, int]:
    """按段序把源快照拼成连续播放时间轴上的字幕并重算分轨。

    跨源合法性（轨道数一致、各段 duration_ms 不小于该源最大 end_ms、
    字幕总数 ≤ 12）在此核对，非法直接抛 422。拼接规则：

    - 字幕起止时间加上之前各段 duration_ms 之和；按段序、段内源顺序排列；
    - id 重写为“段号:段内序号”（均从 1 起），speaker 不变；
    - 源实际分轨写入各条 locked_track，目标沿用共同 track_count 与既有
      最少换轨、并列规则重算——源分轨在平移后仍互不冲突且与锁定一致，
      求解结果必然等于各源画面轨道；changes 按拼接后同一说话人时间相邻
      字幕计算换轨总数（跨段相邻也计入）。
    返回 (字幕, 轨道, changes, track_count)。
    """
    track_count = records[0].track_count
    for idx, record in enumerate(records):
        if record.track_count != track_count:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "各源场景的轨道数（track_count）必须相同",
                    "segment": idx + 1,
                    "track_count": record.track_count,
                    "expected_track_count": track_count,
                },
            )
        max_end = max(
            (s["end_ms"] for s in record.subtitles), default=0
        )
        if durations[idx] < max_end:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "duration_ms 不得小于该源场景字幕的最大 end_ms"
                    ),
                    "segment": idx + 1,
                    "duration_ms": durations[idx],
                    "max_end_ms": max_end,
                },
            )

    total_subtitles = sum(len(record.subtitles) for record in records)
    if total_subtitles > 12:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "拼接后的字幕总数不得超过 12",
                "total_subtitles": total_subtitles,
            },
        )

    subs: List[Subtitle] = []
    offset = 0
    for seg_idx, record in enumerate(records):
        track_by_id = {a["id"]: a["track"] for a in record.assignments}
        for within, s in enumerate(record.subtitles, start=1):
            subs.append(
                Subtitle(
                    id=f"{seg_idx + 1}:{within}",
                    speaker=s["speaker"],
                    start_ms=s["start_ms"] + offset,
                    end_ms=s["end_ms"] + offset,
                    locked_track=track_by_id[s["id"]],
                )
            )
        offset += durations[seg_idx]

    tracks, changes = solve(subs, track_count)
    if tracks is None:  # 理论不可达：各源分轨平移后仍是互不冲突的合法解
        raise RuntimeError("拼接场景在固定各源分轨后无解")
    return subs, tracks, changes, track_count


@app.post("/scenes/concat", response_model=SceneResponse)
def concat_scenes(req: ConcatRequest) -> SceneResponse:
    """按给定顺序把 2～4 个已保存场景拼成连续播放场景。

    各段给出非空 scene_id、正整数 expected_revision 与正整数
    duration_ms；同一个源场景可以重复出现。读取源快照时按段序逐一
    核对存在性与版本：源不存在返回 404，版本不符返回 409。随后核对
    跨源约束：各源 track_count 必须相同、每段 duration_ms 不得小于
    该源字幕的最大 end_ms、拼接后字幕总数 ≤ 12，否则 422。

    拼接结果的字幕起止时间加上之前各段 duration_ms 之和，按段序、
    段内源顺序排列；id 为“段号:段内序号”（均从 1 起），speaker
    不变，源实际分轨写入 locked_track，目标沿用共同轨道数重算，
    changes 按拼接后同一说话人时间相邻字幕统计（跨段相邻计入）。

    提交核对与目标创建在同一个 BEGIN IMMEDIATE 事务内一同生效：
    事务内再核对全部源版本并确认目标未占用，任一失败都回滚——失败
    不留目标，不改任何源场景或播放会话。成功保存为 revision=1 的
    全新场景并返回完整快照，可直接用既有 GET/PUT /scenes/... 与
    /sessions 读取、修订与播放，旧接口不变。
    """
    store = get_store()
    records: List[SceneRecord] = []
    # 读取即核对：按段序逐个读取，拼接所用的字幕与源分轨全部取自与各段
    # expected_revision 一致的完整快照；不存在 404、版本不符立即 409，
    # 绝不拿错配版本拼接。
    for idx, segment in enumerate(req.segments):
        record = store.get_scene(segment.scene_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"场景 {segment.scene_id!r} 不存在",
            )
        if record.revision != segment.expected_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "场景版本冲突：expected_revision 与当前版本不一致"
                    ),
                    "scene_id": segment.scene_id,
                    "segment": idx + 1,
                    "current_revision": record.revision,
                },
            )
        records.append(record)

    durations = [segment.duration_ms for segment in req.segments]
    subs, tracks, changes, track_count = _build_concat(records, durations)

    subtitles_data = [
        {
            "id": s.id,
            "speaker": s.speaker,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "locked_track": s.locked_track,
        }
        for s in subs
    ]
    assignments_data = [
        {"id": s.id, "track": t} for s, t in zip(subs, tracks)
    ]

    try:
        target_record = store.commit_concat(
            target_scene_id=req.target_scene_id,
            sources=[
                (segment.scene_id, segment.expected_revision)
                for segment in req.segments
            ],
            track_count=track_count,
            subtitles=subtitles_data,
            assignments=assignments_data,
            changes=changes,
        )
    except SourceSceneMissing:
        # 源在读取后、事务内被删除（正常流程中场景不可删除）：按 404 处理
        raise HTTPException(status_code=404, detail="源场景不存在")
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )
    except TargetSceneExists as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "target_scene_id 已被占用，拼接目标必须使用新的 scene_id",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )

    return _record_to_scene_response(target_record)


# ---------------------------------------------------------------------------
# 播放会话：创建 / 推进 / 事件推进 / 临时隐藏说话人 / 补取通道 / 接纳 / 结束
# ---------------------------------------------------------------------------


@app.post("/sessions", response_model=SessionStateResponse)
def create_session(req: SessionCreateRequest) -> SessionStateResponse:
    """按 scene_id 创建播放会话。

    复制场景当前的完整快照（字幕、分轨、changes、源版本），游标从
    0 毫秒开始。会话为单进程内存态，不跨重启保留；场景不存在返回 404。
    """
    _check_scene_id(req.scene_id)
    record = get_store().get_scene(req.scene_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"场景 {req.scene_id!r} 不存在"
        )
    return SessionStateResponse(**get_playback().create(record))


def _session_gone_404(session_id: str) -> HTTPException:
    """未知或已结束会话统一返回 404（结束后不保留任何结束记录）。"""
    return HTTPException(
        status_code=404, detail=f"播放会话 {session_id!r} 不存在"
    )


def _channel_full_503(exc: ChannelCapacityExhausted) -> HTTPException:
    """共享补取容量不足：本次生产完整失败（503，可在确认/结束后重试）。

    游标、隐藏集合、seq 与通道队列均未改变；客户端确认并删除部分前缀
    （fetch 提交新的 ack_seq）或结束其他会话释放容量后，用原请求重试即可。
    """
    return HTTPException(
        status_code=503,
        detail={
            "message": (
                "补取通道未确认事件总量已达上限：本次事件无法完整入队，"
                "状态未改变；确认前缀或结束会话释放容量后可重试原请求"
            ),
            "limit": exc.limit,
            "requested": exc.requested,
            "available": exc.available,
        },
    )


# 自动播放状态迁移冲突 -> HTTP 409 的原因与提示文案。
_AUTOPLAY_CONFLICT_MESSAGES = {
    "channel_missing": "启动自动播放前必须先开启补取通道",
    "already_started": "自动播放仅限从未启动的会话启动一次，暂停或阻塞后请用恢复",
    "not_started": "会话尚未启动自动播放",
    "pause_only_running": "暂停仅限运行中的自动播放会话",
    "resume_paused_or_blocked": "恢复仅限暂停或阻塞中的自动播放会话",
    "manual_advance_while_running": (
        "自动播放运行中由后台按真实时间推进，不能手动推进；可先暂停"
    ),
}


def _autoplay_conflict_409(exc: AutoplayConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "message": _AUTOPLAY_CONFLICT_MESSAGES.get(
                exc.reason, "自动播放状态迁移冲突"
            ),
            "reason": exc.reason,
            "status": exc.status,
        },
    )


def _get_session_or_404(session_id: str) -> PlaybackSession:
    session = get_playback().get(session_id)
    if session is None:
        raise _session_gone_404(session_id)
    return session


@app.post("/sessions/{session_id}/advance", response_model=AdvanceResponse)
def advance_session(session_id: str, req: AdvanceRequest) -> AdvanceResponse:
    """推进游标到 time_ms（非负整数，只能不减）。

    返回源版本、游标，以及游标时刻处于 [start_ms, end_ms) 区间的字幕
    及其轨道。倒退返回 422 且会话不变；未知或已结束会话返回 404。
    自动播放运行中的会话由后台按真实时间驱动，手动推进返回 409
    （未启动/暂停/阻塞时仍可手动推进，阻塞满额时仍按 503 失败）。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().advance(session, req.time_ms)
    except SessionFinished:
        # 已取得引用但轮到生效时会话已被结束：与未知会话同等待遇
        raise _session_gone_404(session_id)
    except AutoplayConflict as exc:
        raise _autoplay_conflict_409(exc)
    except CursorRegression as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "time_ms 只能不减，不得小于当前游标",
                "cursor_ms": exc.cursor_ms,
                "time_ms": exc.time_ms,
            },
        )
    except ChannelCapacityExhausted as exc:
        # 共享容量不足：游标未推进、seq/队列未变，客户端可释放容量后重试
        raise _channel_full_503(exc)
    return AdvanceResponse(**state)


@app.post("/sessions/{session_id}/events", response_model=AdvanceEventsResponse)
def advance_session_events(
    session_id: str, req: AdvanceRequest
) -> AdvanceEventsResponse:
    """推进游标并返回本次跨过的全部字幕进入/退出事件。

    与 /advance 共用同一会话游标，同样只能不减；额外返回事件窗口
    （旧游标, time_ms] 内的全部事件（旧游标 < 事件时刻 <= time_ms）：
    字幕在 start_ms 进入、end_ms 退出，短字幕即使开始和结束都落在
    两次采样之间，也会在跨过它的这次推进中成对返回。0 毫秒已活动的
    字幕（start_ms 为 0）由创建会话响应的快照确定，不产生进入事件。

    事件按时刻升序；同一时刻先退出后进入；同类型按会话字幕顺序。
    响应还含推进后的活动字幕（[start_ms, end_ms)）、源版本与游标。
    等时推进（time_ms 等于当前游标）事件为空。事件生成与游标更新
    在会话锁内与推进、接纳、结束完整串行；事件不保存历史、不提供重放。

    倒退或非法时间返回 422 且会话不变；未知或已结束会话返回 404。
    自动播放运行中的会话手动事件推进返回 409。
    已开启通道且本次事件无法完整申请到共享容量时返回 503：游标不推进、
    seq 不分配、事件不部分入队，释放容量后可用同一请求重试。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().advance_events(session, req.time_ms)
    except SessionFinished:
        raise _session_gone_404(session_id)
    except AutoplayConflict as exc:
        raise _autoplay_conflict_409(exc)
    except CursorRegression as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "time_ms 只能不减，不得小于当前游标",
                "cursor_ms": exc.cursor_ms,
                "time_ms": exc.time_ms,
            },
        )
    except ChannelCapacityExhausted as exc:
        raise _channel_full_503(exc)
    return AdvanceEventsResponse(**state)


@app.post(
    "/sessions/{session_id}/hidden_speakers",
    response_model=HiddenSpeakersResponse,
)
def set_hidden_speakers(
    session_id: str, req: HiddenSpeakersRequest
) -> HiddenSpeakersResponse:
    """临时隐藏指定说话人的字幕（仅影响该会话，不持久化）。

    请求体为 `{"hidden_speakers": [...]}`：无重复非空字符串数组，按
    speaker 精确匹配；空数组清除隐藏；允许包含尚未在会话字幕中出现的
    说话人。游标不移动，以当前游标的左闭右开活动区间
    `[start_ms, end_ms)` 判断显示变化：

    - 原可见而现隐藏的活动字幕发 `exit`，原隐藏而现可见者发 `enter`；
    - 事件沿用事件推进的字段（id/speaker/track/time_ms/type），
      `time_ms` 为当前游标，先 `exit` 后 `enter`，同类按会话字幕顺序；
      集合无变化则事件为空；
    - 返回接纳后的隐藏集合、游标、源版本、切换后的可见活动字幕与事件。

    设置后，推进（/advance）与事件推进（/events）的 `active` 与
    `events` 均排除隐藏者，但游标照常推进；隐藏默认空，接纳场景修订后
    仍保留；完整快照（创建/接纳/finish）、冻结规则与分轨不受影响。
    切换与推进、事件推进、接纳、结束在会话锁内串行生效。

    未知或已结束会话返回 404；非法集合（非数组、空字符串、重复、
    元素非字符串）返回 422；已开启通道但显示变更事件无法完整申请到
    共享容量时返回 503（隐藏集合不改变、seq 不分配、事件不入队）；
    任何失败都不改变会话状态。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().set_hidden_speakers(session, req.hidden_speakers)
    except SessionFinished:
        # 已取得引用但轮到生效时会话已被结束：与未知会话同等待遇
        raise _session_gone_404(session_id)
    except ChannelCapacityExhausted as exc:
        raise _channel_full_503(exc)
    return HiddenSpeakersResponse(**state)


@app.post("/sessions/{session_id}/channel", response_model=ChannelOpenResponse)
def open_session_channel(session_id: str) -> ChannelOpenResponse:
    """为播放会话开启补取通道（单进程内存态，每会话最多一个）。

    推进响应丢失后，显示端可经通道补取未确认的字幕变更：开启响应返回
    当前可见 active（与 /advance 相同、排除隐藏说话人）与序号 0——开启
    之前的事件不补记，客户端以本响应的 active 作为初始可见集合。此后
    普通推进、事件推进与隐藏切换产生的全部 enter/exit 按事件字段与排序
    入队，seq 从 1 起连续编号，短字幕不漏记，接纳不重写已排队内容。

    重复开启返回 409（已有通道不变）；未知或已结束会话返回 404。开启与
    既有会话操作在会话锁内完整串行。开启只建立空队列、不占用共享容量
    （容量由此后的生产操作按实际事件数申请）。通道仅内存态：不跨重启
    恢复、不做后台推送；结束会话即清除并释放其占用的全部共享容量。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().open_channel(session)
    except SessionFinished:
        raise _session_gone_404(session_id)
    except ChannelExists:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该会话已开启补取通道，每个播放会话最多一个",
                "session_id": session_id,
            },
        )
    return ChannelOpenResponse(**state)


@app.post(
    "/sessions/{session_id}/channel/fetch",
    response_model=ChannelFetchResponse,
)
def fetch_session_channel(
    session_id: str, req: ChannelFetchRequest
) -> ChannelFetchResponse:
    """补取未确认的字幕变更并确认已收到的事件前缀。

    请求体 `{"ack_seq": N}`：N 为严格非负整数（布尔值与字符串一律
    422）。只能确认此前补取已返回的最大 seq 以内的前缀：越界返回 422
    且通道不变；小于等于已确认位置的旧确认不回退。成功响应返回确认
    位置 acked_seq 与剩余未确认事件（seq > acked_seq，按 seq 递增）；
    未确认事件重复补取时内容与序号不变——推进响应丢失后用上次确认的
    ack_seq 重试即可拿到完全相同的剩余事件。只有“确认并删除前缀”才按
    删除事件数释放共享容量：纯读取与旧确认不释放；满额导致生产得到
    503 后，确认前缀即可腾出容量并用原请求重试。

    未开启通道、未知或已结束会话均返回 404；补取与开启及既有会话操作
    在会话锁内完整串行，任何失败都不改会话或通道。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().fetch_channel(session, req.ack_seq)
    except SessionFinished:
        raise _session_gone_404(session_id)
    except ChannelMissing:
        raise HTTPException(
            status_code=404,
            detail=f"播放会话 {session_id!r} 尚未开启补取通道",
        )
    except AckOutOfRange as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "ack_seq 越界：只能确认此前补取已返回的最大 seq 以内的前缀"
                ),
                "ack_seq": exc.ack_seq,
                "max_returned_seq": exc.max_returned_seq,
            },
        )
    return ChannelFetchResponse(**state)


# ---------------------------------------------------------------------------
# 自动播放：按真实经过时间推进（HTTP 启动/暂停/恢复/查询）
# ---------------------------------------------------------------------------


def _autoplay_call(session_id: str, action) -> AutoplayStateResponse:
    """执行一个自动播放控制动作并统一映射会话级异常。"""
    session = _get_session_or_404(session_id)
    try:
        state = action(get_playback(), session)
    except SessionFinished:
        raise _session_gone_404(session_id)
    except AutoplayConflict as exc:
        raise _autoplay_conflict_409(exc)
    return AutoplayStateResponse(**state)


@app.post(
    "/sessions/{session_id}/autoplay/start",
    response_model=AutoplayStateResponse,
)
def start_session_autoplay(session_id: str) -> AutoplayStateResponse:
    """启动自动播放：让会话按真实经过时间自动推进，客户端不再逐次提交。

    仅限**已开启补取通道且从未启动**（idle）的会话：成功后状态为
    running，进程内后台驱动每 50 毫秒尝试推进一次，目标为本段起始游标
    （即当前游标）加单调时钟经过的整毫秒。计时段起点在会话锁内、本次
    启动串行生效的同一时刻读取：若同会话的接纳等操作正持锁，本请求在
    锁外等待的时间不计入计时，生效后不会把等锁间隔额外推进到游标上、
    也不会提前入队字幕事件。后台推进沿用事件窗口、隐藏过滤、排序、
    seq 与共享容量的全部规则；一次推进整批入队，共享容量不足时整批
    转为 blocked（游标、seq 与队列均不变），须确认释放容量后显式恢复，
    不追赶停顿时间。

    - 未开启补取通道或会话已启动（含运行/暂停/阻塞）：409；
    - 未知或已结束会话：404。
    """
    return _autoplay_call(session_id, lambda pb, s: pb.start_autoplay(s))


@app.post(
    "/sessions/{session_id}/autoplay/pause",
    response_model=AutoplayStateResponse,
)
def pause_session_autoplay(session_id: str) -> AutoplayStateResponse:
    """暂停自动播放：仅限 running；暂停期间不计时。

    保留游标停留在后台最近一次成功推进的值；暂停返回后在途后台任务进锁
    即见非 running，不会再推进本会话。未启动返回 409；暂停或阻塞中再次
    暂停返回 409；未知或已结束会话返回 404。
    """
    return _autoplay_call(session_id, lambda pb, s: pb.pause_autoplay(s))


@app.post(
    "/sessions/{session_id}/autoplay/resume",
    response_model=AutoplayStateResponse,
)
def resume_session_autoplay(session_id: str) -> AutoplayStateResponse:
    """恢复自动播放：仅限 paused 或 blocked；从保留游标重新计时。

    以保留游标作为新一段起点重新记录单调时钟，不追赶暂停或阻塞期间流逝
    的时间；新一段的时钟起点在会话锁内、本次恢复串行生效的同一时刻
    读取——本请求在锁外等待（例如同会话的接纳正持锁）的时间同样不计入。
    阻塞恢复后若容量仍不足，下一拍会再次回到 blocked。未启动或运行中
    返回 409；未知或已结束会话返回 404。
    """
    return _autoplay_call(session_id, lambda pb, s: pb.resume_autoplay(s))


@app.get(
    "/sessions/{session_id}/autoplay",
    response_model=AutoplayStateResponse,
)
def get_session_autoplay(session_id: str) -> AutoplayStateResponse:
    """查询自动播放状态：返回 idle/running/paused/blocked 与当前游标。

    未启动的会话返回 idle；未知或已结束会话返回 404。
    """
    return _autoplay_call(session_id, lambda pb, s: pb.autoplay_status_of(s))


@app.post("/sessions/{session_id}/accept", response_model=SessionStateResponse)
def accept_scene_revision(session_id: str) -> SessionStateResponse:
    """接纳场景当前快照，让播放中的会话跟上后续修订。

    场景读取、已开始字幕校验、分轨重算与会话替换在同一会话锁内作为
    完整操作串行生效：并发接纳时，每个请求接纳的都是轮到它生效时的
    场景快照，迟到的请求不会把会话回写到旧版本。接纳与结束同样串行：
    已结束（含在结束之后轮到生效）的接纳返回 404。

    - 轨道数不得变；
    - 会话中 start_ms <= 游标的字幕，其 id、speaker、起止时间、
      locked_track 必须不变，并固定原分轨；
    - 其余字幕可改、可删、可新增，但都必须在游标之后开始；
    - 按新快照顺序与最少换轨、并列规则重算全部分轨。

    冲突或无解返回 409 且会话不变；成功则整体替换会话快照与源版本
    （场景本身不变），返回会话最新完整状态。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().accept(
            session, lambda: get_store().get_scene(session.scene_id)
        )
    except SessionFinished:
        raise _session_gone_404(session_id)
    except SceneUnavailable:
        raise HTTPException(
            status_code=404, detail=f"场景 {session.scene_id!r} 不存在"
        )
    except AcceptConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "scene_id": session.scene_id},
        )
    return SessionStateResponse(**state)


@app.post("/sessions/{session_id}/finish", response_model=SessionStateResponse)
def finish_session(session_id: str) -> SessionStateResponse:
    """播放结束：取回最终完整快照并释放会话（一次完整操作）。

    在会话锁内把会话标记为已结束并从注册表移除，与同一会话的推进、
    事件推进、隐藏切换、接纳完整串行：

    - 成功响应沿用创建会话的全部字段（session_id、scene_id、
      source_revision、cursor_ms、track_count、changes、subtitles、
      assignments），快照取自轮到结束生效时的会话状态；
    - 快照只来自会话内存状态，不读取场景存储，因此不会纳入尚未被
      接纳的场景修订；
    - 未知会话返回 404；并发结束最多一次成功，落败方与结束后针对
      该 id 的一切请求（含此前已取得会话引用、在锁上等待的在途
      推进/事件推进/接纳）均返回 404；
    - 不改动源场景、其他会话或此前返回的快照；不保存结束记录，
      会话不跨重启恢复（重启后同样按未知会话返回 404）；
    - 已开启补取通道时，结束还会一次性释放该通道剩余未确认事件占用的
      共享容量；重复或并发结束最多一次成功，容量不会重复释放。
    """
    try:
        state = get_playback().finish(session_id)
    except (SessionMissing, SessionFinished):
        raise _session_gone_404(session_id)
    return SessionStateResponse(**state)


# ---------------------------------------------------------------------------
# 播放存档（checkpoint）：SQLite 存档跨重启恢复，保留未确认显示事件
# ---------------------------------------------------------------------------


_CHECKPOINT_CONFLICT_MESSAGES = {
    "channel_missing": "保存播放存档前必须先开启补取通道",
    "autoplay_running": (
        "自动播放运行中的会话不可存档：请先暂停（暂停/阻塞状态可以存档，"
        "恢复后以暂停态续播）"
    ),
}


def _check_checkpoint_id(checkpoint_id: str) -> None:
    """checkpoint_id 路径参数合法性：非空且不得为纯空白（非法 -> 422）。"""
    if checkpoint_id.strip() == "":
        raise HTTPException(
            status_code=422, detail="checkpoint_id 不能为空或纯空白"
        )


def _save_checkpoint_core(session_id: str, checkpoint_id: str) -> dict:
    """存档保存的公共实现（供具名 PUT/POST 与集合式 POST 共用）。

    与同会话的一切操作在会话锁内串行序列化；序列化成功后写入 SQLite
    （BEGIN IMMEDIATE 占用 checkpoint_id），写入失败时会话侧没有任何
    变更，因此失败天然“不留存档、不改会话”。成功不结束会话。
    """
    _check_checkpoint_id(checkpoint_id)
    session = _get_session_or_404(session_id)
    try:
        payload = get_playback().checkpoint_state(session)
    except SessionFinished:
        # 已取得引用但轮到生效时会话已被结束：与未知会话同等待遇
        raise _session_gone_404(session_id)
    except CheckpointNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": _CHECKPOINT_CONFLICT_MESSAGES.get(
                    exc.reason, "当前会话状态不允许保存播放存档"
                ),
                "reason": exc.reason,
                "status": exc.status,
                "session_id": session_id,
                "checkpoint_id": checkpoint_id,
            },
        )

    try:
        get_store().save_checkpoint(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            payload=payload,
        )
    except CheckpointExists:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "checkpoint_id 已被占用，播放存档必须使用新的 id",
                "checkpoint_id": checkpoint_id,
                "session_id": session_id,
            },
        )

    channel = payload["channel"]
    return {
        "checkpoint_id": checkpoint_id,
        "session_id": session_id,
        "scene_id": payload["scene_id"],
        "source_revision": payload["source_revision"],
        "cursor_ms": payload["cursor_ms"],
        "status": payload["autoplay_status"],
        "autoplay_status": payload["autoplay_status"],
        "next_seq": channel["next_seq"],
        "acked_seq": channel["acked_seq"],
        "max_returned_seq": channel["max_returned_seq"],
        "queued_events": len(channel["events"]),
    }


@app.put(
    "/sessions/{session_id}/checkpoints/{checkpoint_id}",
    response_model=CheckpointSaveResponse,
)
@app.post(
    "/sessions/{session_id}/checkpoints/{checkpoint_id}",
    response_model=CheckpointSaveResponse,
)
def save_session_checkpoint(
    session_id: str, checkpoint_id: str
) -> CheckpointSaveResponse:
    """按 session_id 与 checkpoint_id 保存 SQLite 播放存档。

    存档在会话锁内序列化，包含完整快照（字幕/分轨/changes/source_revision/
    track_count）、游标、隐藏集合、补取通道未确认事件及
    next_seq、acked_seq、max_returned_seq 序号边界，与同会话操作串行。
    存档写入与场景同一个 SQLite（同一具名数据卷，跨容器重启可读）。

    - 仅**已开启补取通道且自动播放不处于运行状态**（idle/paused/blocked）
      的会话可存档；未开通道（reason=channel_missing）或自动播放运行中
      （reason=autoplay_running）返回 **409**；
    - 成功 **200** 不结束会话：原会话、游标、隐藏集合与通道队列完全不变；
    - checkpoint_id 为空或纯空白返回 **422**；未知或已结束会话返回 **404**；
      同名存档返回 **409**（先存的那份保留，本次失败不留存档）；
    - 任何失败都不改会话、不留存档（通道容量也不变化）。
    """
    return CheckpointSaveResponse(
        **_save_checkpoint_core(session_id, checkpoint_id)
    )


class CheckpointCreateBody(BaseModel):
    """集合式存档请求体：非空 checkpoint_id（与路径参数等价的便捷写法）。"""

    checkpoint_id: str = Field(
        ..., description="存档 id：非空且尚未被占用"
    )

    @field_validator("checkpoint_id")
    @classmethod
    def _checkpoint_id_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("checkpoint_id 不能为空或纯空白")
        return v


@app.post(
    "/sessions/{session_id}/checkpoints",
    response_model=CheckpointSaveResponse,
)
def create_session_checkpoint(
    session_id: str, req: CheckpointCreateBody
) -> CheckpointSaveResponse:
    """集合式存档：`POST /sessions/{id}/checkpoints`，请求体给出 checkpoint_id。

    语义与 `PUT/POST /sessions/{id}/checkpoints/{checkpoint_id}` 完全相同：
    非空非纯空白 id，其余 404/409 语义一致，成功不结束会话。
    """
    return CheckpointSaveResponse(
        **_save_checkpoint_core(session_id, req.checkpoint_id)
    )


def _restore_checkpoint(checkpoint_id: str) -> dict:
    """按存档恢复为新会话的公共实现（不读取当前场景）。"""
    _check_checkpoint_id(checkpoint_id)
    record = get_store().get_checkpoint(checkpoint_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"播放存档 {checkpoint_id!r} 不存在"
        )
    try:
        return get_playback().restore_from_checkpoint(
            checkpoint_id=record.checkpoint_id,
            source_session_id=record.session_id,
            payload=record.payload,
        )
    except ChannelCapacityExhausted as exc:
        # 队列计入共享容量，不足 503：未注册会话、未留容量占用，可在
        # 确认前缀/结束会话释放容量后用同一存档重新恢复。
        raise _channel_full_503(exc)


@app.post(
    "/checkpoints/{checkpoint_id}/restore",
    response_model=CheckpointRestoreResponse,
)
def restore_checkpoint(checkpoint_id: str) -> CheckpointRestoreResponse:
    """按存档 ID 恢复为**全新 session_id** 的播放会话（不读取当前场景）。

    完整快照、游标、隐藏集合、通道未确认事件与 next_seq/acked_seq/
    max_returned_seq 确认/序号边界全部取自存档：之后新产生的通道事件
    接着存档的 next_seq 连续编号，补取的越界/不回退语义不变。存档中的
    未确认事件数原子计入共享容量——不足返回 **503**，且不创建会话、
    不留任何容量占用；并发恢复由容量原子记账串行化，合计不可能超额。

    自动播放状态：存档时未启动（idle）的恢复后仍未启动；暂停（paused）
    或阻塞（blocked）的恢复为暂停（paused），需显式 resume 才续播，
    并以存档保留的游标重新读取单调时钟开新计时段，**不追赶**离线时间。

    同一份存档可重复恢复为多个相互独立的会话：恢复与其后任何会话操作
    都不修改存档或其他会话。checkpoint_id 非法（空/纯空白）返回 422；
    未知存档返回 404。响应含新会话 id、完整快照与播放状态。
    """
    return CheckpointRestoreResponse(**_restore_checkpoint(checkpoint_id))


@app.post(
    "/sessions/checkpoints/{checkpoint_id}/restore",
    response_model=CheckpointRestoreResponse,
)
def restore_checkpoint_alias(checkpoint_id: str) -> CheckpointRestoreResponse:
    """`/checkpoints/{checkpoint_id}/restore` 的会话命名空间别名（语义相同）。"""
    return CheckpointRestoreResponse(**_restore_checkpoint(checkpoint_id))
