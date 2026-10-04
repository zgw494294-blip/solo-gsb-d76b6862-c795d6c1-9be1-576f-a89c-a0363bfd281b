"""播放会话：单进程内存态，让已保存场景在播放中接纳后续修订。

会话在创建时复制场景当时的完整快照（字幕、分轨、changes、源版本），
游标从 0 毫秒开始，之后支持两种操作：

- 推进（advance）：提交非负整数 time_ms，游标只能不减；返回源版本、
  游标，以及该时刻处于活动区间 [start_ms, end_ms)（左闭右开）的字幕
  及其轨道。
- 事件推进（advance_events）：同样提交非负整数 time_ms 推进游标，
  额外返回“旧游标 < 事件时刻 <= time_ms”窗口内的全部进入（enter）/
  退出（exit）事件——一次推进跨过整条短字幕也不会漏掉。事件按时刻
  升序，同刻先退出后进入，同类按会话字幕顺序。0 毫秒已活动的字幕由
  创建响应的快照确定，不产生进入事件。事件只随本次推进生成，不保存
  历史、不提供重放。
- 临时隐藏说话人（set_hidden_speakers）：按 speaker 精确匹配隐藏字幕，
  仅作用于本会话的内存状态，默认空、不持久化。设置时游标不变，以当前
  游标的左闭右开活动区间判断显示差异：原可见而现隐藏的活动字幕发
  exit，原隐藏而现可见者发 enter；事件沿用原事件字段、时刻取当前游标，
  先 exit 后 enter，同类按会话字幕顺序，集合无变化则无事件。设置后，
  推进与事件推进的 active、events 均排除隐藏者，但游标照常推进；接纳
  不改变隐藏集合。完整快照（state()/finish）与分轨不受影响。
- 接纳（accept）：把场景当前快照并入会话。轨道数不得变；游标前已开始
  （start_ms <= 游标）的字幕，其 id、speaker、起止时间、locked_track
  必须不变，并固定原分轨；其余字幕可改、可删、可新增，但都必须在游标
  之后开始。随后按新快照顺序，以最少换轨与并列规则重算全部分轨。
  冲突或无解即失败且会话保持不变；成功则整体替换会话快照与源版本，
  场景本身不受影响。
- 结束（finish）：一次完整操作取回会话最终完整快照并把会话从注册表
  移除。结束与同一会话的推进、事件推进、隐藏切换、接纳在会话锁内串行
  生效；快照只取自轮到结束生效时的会话内存状态，不读取场景、因此不会
  纳入尚未接纳的场景修订。成功响应沿用创建会话的全部字段。结束后该会话
  ID 的一切请求（含已取得会话引用、在锁上等待的在途请求）均返回 404，
  并发结束最多一次成功。结束不改动源场景、其他会话或此前返回的快照，
  不保存结束记录，也不跨重启恢复。结束同时清除该会话的补取通道。
- 补取通道（channel）：每个会话可开启一个内存通道（重复开启 409），
  开启响应给出当前可见 active 与序号 0；此后普通推进、事件推进与隐藏
  切换产生的全部 enter/exit 按事件字段与排序入队，seq 从 1 连续编号，
  短字幕不漏记，接纳不重写已排队内容。补取提交 ack_seq（严格非负整数），
  只能确认此前补取已返回的最大 seq 以内的前缀（越界 422，旧确认不回退），
  返回确认位置与剩余未确认事件（按 seq 递增，重复补取内容与序号不变）。
  状态变更与入队在同一会话锁内一同生效，开启、补取与既有会话操作完整
  串行，失败不改会话或通道。通道仅内存态：不跨重启恢复、不做后台推送。
  通道共享容量由 CHANNEL_EVENT_LIMIT 配置（正整数，缺省 1024，非法配置
  拒绝启动，可经 Compose 环境变量配置）：它是单进程内所有会话未确认
  事件总量的上限。普通推进、事件推进与隐藏切换按本次实际生成的事件数
  原子申请共享容量，申请与状态变更、seq 分配、完整入队一同生效；容量
  不足立即抛 ChannelCapacityExhausted（HTTP 503），不推进游标、不改变
  隐藏集合、不分配/消费 seq、不部分入队。未开启通道的操作不占容量，
  无事件操作（0 个名额）在满额时仍可执行。容量仅在补取确认并删除前缀
  时按删除数量释放（纯读取与旧确认不释放），或在结束会话时释放该通道
  的剩余队列（重复/并发结束只释放一次）；释放后原请求可原样重试。
- 自动播放（autoplay）：已开启补取通道且从未启动的会话可启动后台自动
  播放，客户端不再逐次提交推进时间。进程内唯一的守护驱动线程每 50ms
  尝试推进一次，目标游标 = 本段起始游标 + 自本段起点单调时钟
  （time.monotonic）经过的整毫秒；暂停、阻塞不计时，恢复以保留游标作为
  新一段起点重新计时，不追赶停顿时间。启动与恢复的单调时钟起点在
  **会话锁内、状态迁移生效的同一时刻**读取：控制请求在锁外排队等待
  （例如同会话的接纳正持锁）的时间不计入计时段，成功生效后不会把等锁
  间隔额外推进到游标上，也不会因此提前入队字幕事件。后台推进沿用事件
  窗口、隐藏过滤、排序、seq 与共享容量的全部既有规则：一次推进整批
  入队，容量不足整批转为 blocked——游标、seq 与队列保持原样，确认
  释放容量后须显式恢复。
  控制接口为启动（start，仅 idle 且已开通道）、暂停（pause，仅 running）、
  恢复（resume，仅 paused/blocked）与查询（status，返回 idle/running/
  paused/blocked 与游标）；状态不符抛 AutoplayConflict（HTTP 409），
  未知会话 404。运行中两种手动推进（advance/advance_events）抛
  AutoplayConflict（409），其他既有会话操作在四种状态下仍可用并完整
  串行。暂停返回后在途后台任务进锁即见非 running，不会再推进；结束会话
  即停止对它的驱动（注册表移除后旧任务取不到它），按原规则释放容量，
  之后一切请求 404。驱动状态仅存于本进程内存，不跨重启，旧接口响应不变。
- 播放存档（checkpoint）：已开启补取通道且自动播放非运行状态的会话可在
  会话锁内把完整快照、游标、隐藏集合、通道未确认事件与
  next_seq/acked_seq/max_returned_seq 序号边界序列化为存档负载（由调用方
  写入 SQLite）；未开通道或自动播放运行中拒绝存档（CheckpointNotAllowed，
  HTTP 409），序列化与同会话一切操作串行，成功不改变、不结束会话。恢复按
  存档负载创建全新 session_id 的会话，不读取当前场景：队列按存档事件数
  原子计入共享容量（不足 ChannelCapacityExhausted，503，不留会话或容量
  占用，并发不超额）；idle 恢复为 idle，paused/blocked 恢复为 paused，
  续播从保留游标重新计时、不追赶离线时间；同一份存档可重复恢复为互不
  影响的独立会话，后续操作不修改存档或其他会话。

会话只存在于本进程内存中，不随重启保留。同一会话上的推进、事件推进、
隐藏切换、接纳、结束与补取通道的开启/补取通过会话级互斥锁串行化：
每次调用要么完整生效，要么完全无效。
接纳是一个完整操作——场景快照的读取（load_record 回调）、已开始字幕
校验、分轨重算与会话替换都在会话锁内完成，因此并发下迟到的接纳请求
读到的一定是轮到它生效时的场景快照，不会把会话回写到进入锁之前读到的
旧版本。结束在会话锁内置位 closed 墓碑并移除注册表条目：先取得会话
引用、在锁上排队的请求轮到它生效时会看到墓碑而失败（404），不会在
结束之后再对已结束的会话生效一次。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .solver import Subtitle, solve
from .storage import SceneRecord


# 自动播放后台尝试推进的节拍（秒）：后台驱动每 50 毫秒尝试推进一次。
AUTOPLAY_TICK_INTERVAL_S = 0.05

# 自动播放状态（驱动状态仅存于本进程内存，不随重启保留）：
# idle 未启动；running 运行中（后台按单调时钟推进游标）；
# paused 已暂停（暂停不计时，恢复从保留游标重新计时）；
# blocked 因补取通道共享容量满额而阻塞（游标、seq 与队列不变，
# 确认释放容量后须显式恢复，不追赶停顿期间流逝的时间）。
AUTOPLAY_IDLE = "idle"
AUTOPLAY_RUNNING = "running"
AUTOPLAY_PAUSED = "paused"
AUTOPLAY_BLOCKED = "blocked"


class AutoplayConflict(Exception):
    """自动播放状态迁移不被允许（对应 HTTP 409）。

    reason 取值：
    - channel_missing：仅已开启补取通道的会话可启动自动播放；
    - already_started：启动仅限从未启动的会话（暂停/阻塞后不能重新启动）；
    - not_started：暂停、恢复仅限已启动的会话；
    - pause_only_running：暂停仅允许运行中（运行态）的会话；
    - resume_paused_or_blocked：恢复仅允许暂停或阻塞的会话。
    """

    def __init__(self, reason: str, status: str) -> None:
        self.reason = reason
        self.status = status
        super().__init__(f"自动播放状态迁移冲突：{reason}（当前状态 {status}）")


# 补取通道共享容量（单进程内所有会话的未确认事件总量）的默认上限。
DEFAULT_CHANNEL_EVENT_LIMIT = 1024


def parse_channel_event_limit(raw: Optional[str]) -> int:
    """解析环境变量 CHANNEL_EVENT_LIMIT：缺省为 1024，否则必须是正整数。

    只接受十进制阿拉伯数字组成的字符串（允许首尾空白）：空值、0、负数、
    小数、布尔/字符串等非数字写法、带正负号或下划线的写法一律拒绝
    （由调用方据此拒绝启动）。
    """
    if raw is None:
        return DEFAULT_CHANNEL_EVENT_LIMIT
    text = raw.strip()
    if not text or any(ch < "0" or ch > "9" for ch in text):
        raise ValueError(
            "CHANNEL_EVENT_LIMIT 必须为正整数（十进制数字），缺省为 "
            f"{DEFAULT_CHANNEL_EVENT_LIMIT}，实际配置为 {raw!r}"
        )
    value = int(text)
    if value <= 0:
        raise ValueError(
            "CHANNEL_EVENT_LIMIT 必须为正整数（≥1），实际配置为 "
            f"{raw!r}"
        )
    return value


# 非法配置拒绝启动：模块导入（即 uvicorn 加载 app）时立即校验，进程直接启动失败。
parse_channel_event_limit(os.environ.get("CHANNEL_EVENT_LIMIT"))


class ChannelEventLimitInvalid(Exception):
    """显式传入的补取通道容量上限非法（构造管理器时拒绝）。"""


class ChannelCapacityExhausted(Exception):
    """共享未确认事件容量不足，本次生产无法完整入队（对应 HTTP 503）。

    状态变更（游标推进、隐藏集合替换）、seq 分配与入队均尚未发生：
    调用方必须在申请到完整容量之前保持会话与通道原样，客户端可在
    确认前缀（fetch 提交新的 ack_seq）或结束会话释放容量后原样重试。
    """

    def __init__(self, limit: int, requested: int, available: int) -> None:
        self.limit = limit
        self.requested = requested
        self.available = available
        super().__init__(
            f"补取通道未确认事件总量已达上限 {limit}：本次需要 "
            f"{requested}，仅剩 {available}"
        )


class ChannelCapacity:
    """单进程内所有补取通道共享的未确认事件容量。

    used 恒等于此刻所有已开启（且未随会话结束释放）通道中尚未确认
    事件的总数：生产（普通推进/事件推进/隐藏切换）按本次实际生成的
    事件数 reserve，确认并删除前缀或结束会话时 release 对应数量。
    reserve 与 release 均在自身锁内原子完成，保证不同会话并发生产
    不会突破总额；本对象不持有任何会话锁，也不会被会话锁反向等待。
    """

    def __init__(self, limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ChannelEventLimitInvalid(
                f"补取通道容量上限必须为正整数，实际为 {limit!r}"
            )
        self._limit = limit
        self._used = 0
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._limit - self._used

    def reserve(self, count: int) -> None:
        """为本次生产原子申请 count 个名额；不足抛 ChannelCapacityExhausted。

        count == 0 时不占用任何名额：无事件的操作在满额时仍可执行。
        """
        with self._lock:
            if self._used + count > self._limit:
                raise ChannelCapacityExhausted(
                    self._limit, count, self._limit - self._used
                )
            self._used += count

    def release(self, count: int) -> None:
        """释放 count 个名额（确认前缀或结束会话时调用）。"""
        if count == 0:
            return
        with self._lock:
            new_used = self._used - count
            if new_used < 0:
                # 容量记账与队列删除成对出现，正常流程不可达
                raise RuntimeError(
                    f"补取通道容量释放超额：已占用 {self._used}，尝试释放 "
                    f"{count}"
                )
            self._used = new_used


class AcceptConflict(Exception):
    """接纳场景当前快照失败（对应 HTTP 409）。

    原因包括：轨道数变化、已开始字幕被修改/删除、新增或修改的字幕
    未在游标之后开始、固定已开始字幕的原分轨后重算无解。
    """


class CursorRegression(Exception):
    """推进时间小于当前游标（对应 HTTP 422）。"""

    def __init__(self, cursor_ms: int, time_ms: int) -> None:
        self.cursor_ms = cursor_ms
        self.time_ms = time_ms
        super().__init__(
            f"time_ms={time_ms} 小于当前游标 cursor_ms={cursor_ms}，游标只能不减"
        )


class SceneUnavailable(Exception):
    """接纳生效时场景已不存在（对应 HTTP 404）。"""

    def __init__(self, scene_id: str) -> None:
        self.scene_id = scene_id
        super().__init__(f"场景 {scene_id!r} 不存在")


class SessionMissing(Exception):
    """注册表中不存在该会话 id（对应 HTTP 404）。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"播放会话 {session_id!r} 不存在")


class SessionFinished(Exception):
    """会话已结束：请求已取得引用但轮到它生效时墓碑已置位（对应 HTTP 404）。

    结束是完整操作：在会话锁内把 closed 置为 True 并移除注册表条目。
    此前已取得 PlaybackSession 引用、正在会话锁上排队的推进/事件推进/
    接纳/结束请求，轮到它生效时看到 closed 即按未知会话处理（404），
    不会在结束之后再对该会话生效一次。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"播放会话 {session_id!r} 已结束")


class ChannelExists(Exception):
    """该会话已开启补取通道，重复开启（对应 HTTP 409）。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"播放会话 {session_id!r} 已开启补取通道")


class ChannelMissing(Exception):
    """该会话尚未开启补取通道（对应 HTTP 404）。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"播放会话 {session_id!r} 尚未开启补取通道")


class AckOutOfRange(Exception):
    """ack_seq 超过此前补取已返回的最大 seq（对应 HTTP 422，通道不变）。"""

    def __init__(self, ack_seq: int, max_returned_seq: int) -> None:
        self.ack_seq = ack_seq
        self.max_returned_seq = max_returned_seq
        super().__init__(
            f"ack_seq={ack_seq} 越界：此前补取已返回的最大 seq 为 "
            f"{max_returned_seq}"
        )


class CheckpointNotAllowed(Exception):
    """当前会话状态不允许保存播放存档（对应 HTTP 409）。

    reason 取值：
    - channel_missing：存档只保存已开启补取通道的会话；
    - autoplay_running：自动播放运行中的会话不允许存档（暂停/阻塞/未启动
      的自动播放可以，恢复后的会话以暂停态续播）。
    """

    def __init__(self, reason: str, status: str) -> None:
        self.reason = reason
        self.status = status
        super().__init__(f"播放存档条件不满足：{reason}（当前状态 {status}）")


@dataclass
class ChannelState:
    """补取通道的内存状态（仅属于会话，不持久化、不跨重启保留）。

    events 为尚未确认的已排队事件：每项含 seq 与事件推进的事件字段
    （id/speaker/track/time_ms/type），按 seq 递增；next_seq 为下一个
    事件序号（从 1 起连续编号）；acked_seq 为确认位置（已确认的最大
    序号，只增不回退）；max_returned_seq 为此前补取响应实际返回过的
    最大 seq——ack_seq 不得超过它，越界按 422 拒绝。
    """

    events: List[Dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    acked_seq: int = 0
    max_returned_seq: int = 0


@dataclass
class PlaybackSession:
    """一次播放会话的内存状态；subtitles 与 tracks 按下标对齐。"""

    session_id: str
    scene_id: str
    source_revision: int
    track_count: int
    subtitles: List[Dict[str, Any]]
    tracks: List[int]
    changes: int
    cursor_ms: int = 0
    # 临时隐藏的说话人集合：按 speaker 精确匹配，仅影响本会话的
    # active/events 视图；默认空，接纳后保留，不随会话持久化。
    hidden_speakers: set = field(default_factory=set)
    # 补取通道：开启后记录普通推进/事件推进/隐藏切换产生的全部
    # enter/exit；仅内存态，未开启为 None，结束会话时清除。
    channel: Optional[ChannelState] = None
    # 结束墓碑：finish 在会话锁内置为 True 并移除注册表条目。此后即使
    # 仍持有本对象引用的在途请求，在锁内读到 closed 也一律按 404 失败。
    closed: bool = False
    # 自动播放驱动状态（仅内存态，不跨重启保留）：
    # autoplay_status 取 idle/running/paused/blocked；base_cursor_ms 为本段
    # 起始游标（启动时的当前游标，或暂停/阻塞保留的游标）；base_monotonic_s
    # 为本段起点的单调时钟读数——在会话锁内、状态迁移生效的同一时刻读取，
    # 控制请求等锁的时间不计入。暂停与阻塞不计时——恢复时以保留游标作为
    # 新一段的起点重新计时，不追赶停顿时间。
    autoplay_status: str = AUTOPLAY_IDLE
    base_cursor_ms: int = 0
    base_monotonic_s: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def active(self) -> List[Dict[str, Any]]:
        """游标时刻处于 [start_ms, end_ms) 内且未隐藏的字幕及轨道。

        隐藏只过滤播放视图：区间判断仍为左闭右开，分轨与完整快照不变。
        """
        hidden = self.hidden_speakers
        return [
            {**sub, "track": track}
            for sub, track in zip(self.subtitles, self.tracks)
            if sub["speaker"] not in hidden
            and sub["start_ms"] <= self.cursor_ms < sub["end_ms"]
        ]

    def state(self) -> Dict[str, Any]:
        """会话完整快照（纯数据，可直接作为响应体）。"""
        return {
            "session_id": self.session_id,
            "scene_id": self.scene_id,
            "source_revision": self.source_revision,
            "cursor_ms": self.cursor_ms,
            "track_count": self.track_count,
            "changes": self.changes,
            "subtitles": [dict(s) for s in self.subtitles],
            "assignments": [
                {"id": s["id"], "track": t}
                for s, t in zip(self.subtitles, self.tracks)
            ],
        }

    def autoplay_state(self) -> Dict[str, Any]:
        """自动播放控制状态（调用方须持有会话锁）。"""
        return {
            "session_id": self.session_id,
            "status": self.autoplay_status,
            "cursor_ms": self.cursor_ms,
        }


def _identity(sub: Dict[str, Any]) -> Tuple[Any, ...]:
    """已开始字幕在接纳时必须保持不变的字段组合。"""
    return (
        sub["id"],
        sub["speaker"],
        sub["start_ms"],
        sub["end_ms"],
        sub.get("locked_track"),
    )


def _window_events(
    session: PlaybackSession, old_cursor: int, time_ms: int
) -> List[Dict[str, Any]]:
    """（旧游标, time_ms] 窗口内的全部进入/退出事件（排除隐藏说话人）。

    字幕在 start_ms 进入、end_ms 退出（区间左闭右开）；一次采样间隔内
    开始又结束的短字幕会同时给出进入与退出，不会漏。排序与事件推进一致：
    时刻升序，同刻先退出后进入，同类按会话字幕顺序。
    """
    # 每项为 (时刻, 同刻次序, 会话顺序, 事件体)；同刻次序令 exit 在前
    pending: List[Tuple[int, int, int, Dict[str, Any]]] = []
    hidden = session.hidden_speakers
    for order, (sub, track) in enumerate(
        zip(session.subtitles, session.tracks)
    ):
        # 隐藏说话人的字幕既不进入 active，也不产生进入/退出事件
        if sub["speaker"] in hidden:
            continue
        start_ms = sub["start_ms"]
        end_ms = sub["end_ms"]
        base = {
            "id": sub["id"],
            "speaker": sub["speaker"],
            "track": track,
        }
        if old_cursor < start_ms <= time_ms:
            pending.append(
                (start_ms, 1, order, {**base, "time_ms": start_ms, "type": "enter"})
            )
        if old_cursor < end_ms <= time_ms:
            pending.append(
                (end_ms, 0, order, {**base, "time_ms": end_ms, "type": "exit"})
            )
    pending.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in pending]


def _channel_append(
    session: PlaybackSession, events: List[Dict[str, Any]]
) -> None:
    """把本次操作产生的事件按序追加到补取通道（未开启则为无操作）。

    调用方须已持有会话锁，且已按 len(events) 从共享容量 reserve 成功：
    因此本函数只做追加，绝不留下“占了容量却没入队”或“入了队却没占容量”
    的中间状态——容量申请与游标推进/隐藏集合替换、seq 分配、完整入队
    对外部是同一次原子生效。seq 从 1 起连续分配；已排队内容自此不被
    改写（接纳场景修订也不影响）。
    """
    channel = session.channel
    if channel is None:
        return
    for event in events:
        channel.events.append({"seq": channel.next_seq, **event})
        channel.next_seq += 1


class PlaybackManager:
    """会话注册表：单进程内存实现，不持久化、不跨重启保留。

    锁序约定为“会话锁 -> 共享容量锁/注册表锁”：所有生产路径在持有
    会话锁时原子申请共享容量，finish 在会话锁内释放容量并取注册表锁
    移除条目；其余路径从不持有容量锁或注册表锁等待会话锁，因此不存在
    反向等待、不会死锁。
    """

    def __init__(
        self,
        channel_event_limit: Optional[int] = None,
        autoplay_tick_interval_s: Optional[float] = AUTOPLAY_TICK_INTERVAL_S,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._sessions: Dict[str, PlaybackSession] = {}
        self._registry_lock = threading.Lock()
        if channel_event_limit is None:
            channel_event_limit = parse_channel_event_limit(
                os.environ.get("CHANNEL_EVENT_LIMIT")
            )
        # 单进程内所有会话的补取通道共用同一份未确认事件容量。
        self._channel_capacity = ChannelCapacity(channel_event_limit)
        # 单调时钟（秒）：启动/恢复在会话锁内、状态迁移生效的同一时刻读取，
        # 在锁外排队等待的时间不计入计时段；测试可注入可控时钟，
        # 生产默认 time.monotonic。
        self._clock = clock if clock is not None else time.monotonic
        # 自动播放后台驱动：进程内唯一的守护线程，每 tick 间隔（默认 50ms）
        # 对所有运行中的会话尝试推进一次。驱动状态只存于本进程内存。
        # autoplay_tick_interval_s=None 表示不启动后台线程（仅显式 tick，
        # 供确定性测试使用）。
        self._autoplay_tick_interval_s = autoplay_tick_interval_s
        self._driver_thread: Optional[threading.Thread] = None
        self._driver_wake = threading.Event()
        self._driver_stop = threading.Event()

    @property
    def channel_capacity(self) -> ChannelCapacity:
        """共享通道容量（测试与启动诊断用；生产路径只经方法操作）。"""
        return self._channel_capacity

    def create(self, record: SceneRecord) -> Dict[str, Any]:
        """按场景当前快照创建会话：复制完整快照，游标从 0 毫秒开始。"""
        subtitles = [dict(s) for s in record.subtitles]
        track_by_id = {a["id"]: a["track"] for a in record.assignments}
        session = PlaybackSession(
            session_id=uuid.uuid4().hex,
            scene_id=record.scene_id,
            source_revision=record.revision,
            track_count=record.track_count,
            subtitles=subtitles,
            tracks=[track_by_id[s["id"]] for s in subtitles],
            changes=record.changes,
        )
        state = session.state()
        with self._registry_lock:
            self._sessions[session.session_id] = session
        return state

    def get(self, session_id: str) -> Optional[PlaybackSession]:
        with self._registry_lock:
            return self._sessions.get(session_id)

    def _advance_locked(
        self, session: PlaybackSession, time_ms: int, include_events: bool
    ) -> Dict[str, Any]:
        """已持有会话锁时的推进实现：事件窗口、容量申请、游标更新原子生效。

        include_events=False（普通推进）时响应不含事件，但已开启补取通道
        仍要完整记录跨过的 enter/exit。容量申请成功之前不做任何状态变更。
        """
        old_cursor = session.cursor_ms
        if session.channel is not None:
            # 普通推进的响应不含事件，但补取通道要完整记录跨过的 enter/exit
            events = _window_events(session, old_cursor, time_ms)
            # 容量申请成功之前不推进游标、不分配/消费 seq、不入队；
            # 0 个事件不占名额，满额时仍可推进。
            self._channel_capacity.reserve(len(events))
        else:
            # 未开启通道：事件推进仍需事件用于响应，普通推进无需计算。
            events = (
                _window_events(session, old_cursor, time_ms)
                if include_events
                else []
            )
        session.cursor_ms = time_ms
        _channel_append(session, events)
        return {
            "session_id": session.session_id,
            "scene_id": session.scene_id,
            "source_revision": session.source_revision,
            "cursor_ms": session.cursor_ms,
            "active": session.active(),
            **({"events": events} if include_events else {}),
        }

    def advance(self, session: PlaybackSession, time_ms: int) -> Dict[str, Any]:
        """推进游标（只能不减），返回源版本、游标与活动字幕及轨道。

        响应保持原样（不含事件）；若已开启补取通道，本次推进跨过的全部
        进入/退出事件（与事件推进同一窗口与排序，短字幕不漏记）与游标
        更新一同生效。生产前先按本次实际生成的事件数从共享容量原子申请：
        容量不足抛 ChannelCapacityExhausted（503），此时尚未推进游标、
        尚未分配/消费 seq、尚未入队，会话与通道保持原样，客户端可重试。

        自动播放运行中（running）的会话由后台按真实时间驱动，手动推进
        抛 AutoplayConflict（409）；未启动、暂停、阻塞时仍可手动推进
        （阻塞时满额同样按 503 失败）。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if session.autoplay_status == AUTOPLAY_RUNNING:
                raise AutoplayConflict(
                    "manual_advance_while_running", session.autoplay_status
                )
            if time_ms < session.cursor_ms:
                raise CursorRegression(session.cursor_ms, time_ms)
            return self._advance_locked(session, time_ms, False)

    def advance_events(
        self, session: PlaybackSession, time_ms: int
    ) -> Dict[str, Any]:
        """推进游标并返回本次跨过的全部进入/退出事件。

        事件窗口为（旧游标, time_ms]：旧游标 < 事件时刻 <= time_ms。
        字幕在 start_ms 进入、end_ms 退出（区间左闭右开）；因此一次
        采样间隔内开始又结束的短字幕会同时给出退出与进入事件，不会漏。
        0 毫秒已活动（start_ms == 0）的字幕由创建会话时的快照确定，
        初始游标即 0，不产生进入事件。事件按时刻升序；同刻先退出后
        进入；同类按会话字幕顺序。time_ms 等于当前游标时事件为空。

        事件生成、游标更新与活动字幕计算基于同一份会话快照，在会话锁
        内一次完成，与推进、接纳、结束、后台自动推进完整串行。倒退抛
        CursorRegression，会话保持不变。事件不保存历史、不提供重放。
        隐藏说话人的字幕不进入 active，也不产生事件；游标仍照常推进。
        若已开启补取通道，本次事件在游标更新的同一会话锁内按序入队：
        入队前先按事件数原子申请共享容量，不足抛 ChannelCapacityExhausted
        （503）——不推进游标、不分配/消费 seq、不入队，会话与通道保持
        原样。自动播放运行中的会话不接受手动事件推进（AutoplayConflict
        -> 409）。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if session.autoplay_status == AUTOPLAY_RUNNING:
                raise AutoplayConflict(
                    "manual_advance_while_running", session.autoplay_status
                )
            if time_ms < session.cursor_ms:
                raise CursorRegression(session.cursor_ms, time_ms)
            return self._advance_locked(session, time_ms, True)

    def set_hidden_speakers(
        self, session: PlaybackSession, hidden_speakers: List[str]
    ) -> Dict[str, Any]:
        """替换本会话临时隐藏的说话人集合，并返回当前游标下的显示变更。

        按 speaker 精确匹配；hidden_speakers 由调用方保证为无重复、非空
        字符串序列（非法集合在 HTTP 层按 422 拒绝，本方法不负责校验），
        允许包含会话字幕中尚未出现（甚至永不出现）的说话人。游标不移动：
        以当前游标的左闭右开活动区间 [start_ms, end_ms) 判断新旧可见性——

        - 旧集合可见、新集合隐藏且此刻活动的字幕，产生 exit；
        - 旧集合隐藏、新集合可见且此刻活动的字幕，产生 enter；
        - 事件沿用事件推进的字段（id/speaker/track/time_ms/type），
          time_ms 取当前游标；先全部 exit 后全部 enter，同类按会话字幕
          顺序；新旧集合下显示状态相同的字幕不产生事件，集合无变化则
          事件为空。

        设置成功后，推进与事件推进的 active、events 都排除隐藏者，但
        游标照常推进；完整快照（state()/finish）、分轨与冻结规则不变。
        设置仅存于会话内存、不持久化，接纳不改变隐藏集合。与推进、事件
        推进、接纳、结束在会话锁内完整串行；会话已结束抛 SessionFinished。
        已开启通道时，显示变更事件与隐藏集合替换一同生效：替换前先按
        事件数原子申请共享容量，不足抛 ChannelCapacityExhausted（503）
        ——隐藏集合不改变、seq 不分配、事件不入队；0 个事件（集合无变化
        或无活动变更）不占名额，满额时仍可提交。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            old_hidden = session.hidden_speakers
            new_hidden = set(hidden_speakers)
            cursor = session.cursor_ms
            exits: List[Tuple[int, Dict[str, Any]]] = []
            enters: List[Tuple[int, Dict[str, Any]]] = []
            for order, (sub, track) in enumerate(
                zip(session.subtitles, session.tracks)
            ):
                speaker = sub["speaker"]
                was_hidden = speaker in old_hidden
                is_hidden = speaker in new_hidden
                if was_hidden == is_hidden:
                    continue
                # 仅对当前游标左闭右开区间内的活动字幕发显示变更事件
                if not (sub["start_ms"] <= cursor < sub["end_ms"]):
                    continue
                event = {
                    "id": sub["id"],
                    "speaker": speaker,
                    "track": track,
                    "time_ms": cursor,
                    "type": "exit" if is_hidden else "enter",
                }
                (exits if is_hidden else enters).append((order, event))
            exits.sort(key=lambda item: item[0])
            enters.sort(key=lambda item: item[0])
            # 先 exit 后 enter；同类已按会话字幕顺序
            events = [item[1] for item in exits] + [
                item[1] for item in enters
            ]
            # 容量申请成功之前不替换隐藏集合、不分配 seq、不入队
            if session.channel is not None:
                self._channel_capacity.reserve(len(events))
            session.hidden_speakers = new_hidden
            # 与隐藏集合的替换一同生效：显示变更事件同步入队（未开启则无操作）
            _channel_append(session, events)
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "cursor_ms": cursor,
                # 回显本次接纳的集合：去重后保持提交顺序
                "hidden_speakers": list(hidden_speakers),
                "events": events,
                "active": session.active(),
            }

    def open_channel(self, session: PlaybackSession) -> Dict[str, Any]:
        """为会话开启补取通道：返回当前可见活动字幕与起始序号 0。

        每个会话最多一个通道，重复开启抛 ChannelExists（409，已有通道
        不变）。开启只建立空队列，不补记开启之前的事件——客户端以本响应
        的 active 作为初始可见集合，此后的 enter/exit 由通道记录。与
        既有会话操作在会话锁内完整串行；会话已结束抛 SessionFinished。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if session.channel is not None:
                raise ChannelExists(session.session_id)
            session.channel = ChannelState()
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "cursor_ms": session.cursor_ms,
                "seq": 0,
                "active": session.active(),
            }

    def fetch_channel(
        self, session: PlaybackSession, ack_seq: int
    ) -> Dict[str, Any]:
        """补取未确认事件并确认已收到的事件前缀。

        ack_seq 只能确认此前补取已返回的最大 seq 以内的前缀：越界抛
        AckOutOfRange（422，通道不变）；小于等于已确认位置的旧确认不
        回退。成功时确认位置前进到 max(acked_seq, ack_seq)，丢弃已确认
        前缀，返回确认位置与剩余未确认事件（按 seq 递增；内容与序号自
        入队起不变，重复补取结果相同），并把“已返回的最大 seq”推进到
        本次返回的末尾。

        共享容量只在“确认并删除前缀”时按本次实际删除的事件数释放：
        纯读取（ack_seq 不超过已确认位置、未删除任何事件）与旧确认不
        释放容量；确认位置推进与前缀删除、容量释放在同一会话锁内完整
        生效。未开启通道抛 ChannelMissing（404）；会话已结束抛
        SessionFinished（404）。与既有会话操作在会话锁内完整串行。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            channel = session.channel
            if channel is None:
                raise ChannelMissing(session.session_id)
            if ack_seq > channel.max_returned_seq:
                raise AckOutOfRange(ack_seq, channel.max_returned_seq)
            if ack_seq > channel.acked_seq:
                channel.acked_seq = ack_seq
            # 删除已确认前缀；仅本次实际删除的事件释放共享容量。
            # 纯读取（剩余为空、ack 未前进或只重复旧确认）不释放。
            dropped = 0
            if channel.events and channel.events[0]["seq"] <= channel.acked_seq:
                kept = [
                    e for e in channel.events if e["seq"] > channel.acked_seq
                ]
                dropped = len(channel.events) - len(kept)
                channel.events = kept
            if dropped:
                self._channel_capacity.release(dropped)
            remaining = [dict(e) for e in channel.events]
            if remaining:
                channel.max_returned_seq = remaining[-1]["seq"]
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "acked_seq": channel.acked_seq,
                "events": remaining,
            }

    # ------------------------------------------------------------------
    # 播放存档（checkpoint）：锁内序列化 / 恢复为新会话（队列计入共享容量）
    # ------------------------------------------------------------------

    def checkpoint_state(self, session: PlaybackSession) -> Dict[str, Any]:
        """在会话锁内把当前播放状态序列化为可持久化的存档负载。

        仅允许**已开启补取通道且自动播放不处于 running** 的会话存档：
        未开启通道抛 CheckpointNotAllowed(channel_missing)；运行中抛
        CheckpointNotAllowed(autoplay_running)；会话已结束抛
        SessionFinished（404）。本方法只做序列化、不写入存储（由调用方
        落库）、不改变任何会话状态——成功存档不结束会话。

        与同会话的推进、事件推进、隐藏切换、接纳、结束、通道开启/补取、
        自动播放控制在会话锁内完整串行，因此序列化出的完整快照、游标、
        隐藏集合、通道未确认事件与 next_seq/acked_seq/max_returned_seq
        序号边界是同一份一致状态；随后的存储写入失败只需不落库即可，
        会话侧没有任何需要回滚的变更。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if session.channel is None:
                raise CheckpointNotAllowed(
                    "channel_missing", session.autoplay_status
                )
            if session.autoplay_status == AUTOPLAY_RUNNING:
                raise CheckpointNotAllowed(
                    "autoplay_running", session.autoplay_status
                )
            channel = session.channel
            return {
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "track_count": session.track_count,
                "changes": session.changes,
                "subtitles": [dict(s) for s in session.subtitles],
                "assignments": [
                    {"id": s["id"], "track": t}
                    for s, t in zip(session.subtitles, session.tracks)
                ],
                "cursor_ms": session.cursor_ms,
                # 集合以列表形式持久化（恢复时重新建 set，顺序无关）。
                "hidden_speakers": list(session.hidden_speakers),
                # 自动播放的驱动状态可存档部分：运行中不允许存档，因此
                # 只会是 idle/paused/blocked；单调时钟读数不持久化，恢复
                # 后未启动/暂停一律从保留游标重新计时，不追赶离线时间。
                "autoplay_status": session.autoplay_status,
                "channel": {
                    "events": [dict(e) for e in channel.events],
                    "next_seq": channel.next_seq,
                    "acked_seq": channel.acked_seq,
                    "max_returned_seq": channel.max_returned_seq,
                },
            }

    def restore_from_checkpoint(
        self,
        checkpoint_id: str,
        source_session_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """按存档负载恢复为一个全新的播放会话（不读取当前场景）。

        完整快照（字幕/分轨/changes/source_revision/track_count）、游标、
        隐藏集合、通道未确认事件与 next_seq/acked_seq/max_returned_seq
        序号边界全部取自存档；新会话获得全新 session_id，与存档源会话及
        其他恢复会话相互独立——同一份存档可重复恢复为多个独立会话，恢复
        及之后的任何会话操作都不修改存档或其他会话。

        通道队列按存档时的未确认事件数计入共享容量：容量不足抛
        ChannelCapacityExhausted（HTTP 503），此时不注册会话、不留任何
        容量占用。容量申请在注册表登记之前原子完成，并发恢复不可能超额。

        自动播放状态映射：存档时未启动（idle）恢复后仍未启动；暂停
        （paused）或阻塞（blocked）恢复为暂停（paused），需要客户端显式
        resume 才重新计时，并从存档保留的游标开新计时段，不追赶离线时间
        （运行中的会话本来就不允许存档）。后续补取与新入队事件的序号
        接着存档的 next_seq 继续编号。
        """
        channel_data = payload["channel"]
        events = channel_data["events"]
        # 先为存档的未确认队列原子申请共享容量：不足直接失败（503），
        # 既不创建会话也不留下容量占用；多个并发恢复由此被串行记账，
        # 合计不可能突破共享上限。
        self._channel_capacity.reserve(len(events))
        try:
            new_id = uuid.uuid4().hex
            subtitles = [dict(s) for s in payload["subtitles"]]
            track_by_id = {a["id"]: a["track"] for a in payload["assignments"]}
            autoplay_status = payload.get("autoplay_status", AUTOPLAY_IDLE)
            if autoplay_status in (AUTOPLAY_PAUSED, AUTOPLAY_BLOCKED):
                restored_status = AUTOPLAY_PAUSED
            else:
                restored_status = AUTOPLAY_IDLE
            cursor_ms = payload["cursor_ms"]
            session = PlaybackSession(
                session_id=new_id,
                scene_id=payload["scene_id"],
                source_revision=payload["source_revision"],
                track_count=payload["track_count"],
                subtitles=subtitles,
                tracks=[track_by_id[s["id"]] for s in subtitles],
                changes=payload["changes"],
                cursor_ms=cursor_ms,
                hidden_speakers=set(payload.get("hidden_speakers", [])),
                channel=ChannelState(
                    events=[dict(e) for e in events],
                    next_seq=channel_data["next_seq"],
                    acked_seq=channel_data["acked_seq"],
                    max_returned_seq=channel_data["max_returned_seq"],
                ),
                # 恢复即暂停/未启动：不启动后台驱动段；resume 时会以当前
                # 游标重新读取单调时钟开新计时段（不追赶离线时间）。
                autoplay_status=restored_status,
                base_cursor_ms=cursor_ms,
                base_monotonic_s=0.0,
            )
            with self._registry_lock:
                self._sessions[new_id] = session
        except BaseException:
            # 构造或登记异常时归还刚申请的容量，杜绝“占用却没有会话”。
            self._channel_capacity.release(len(events))
            raise
        state = session.state()
        state.update(
            {
                "checkpoint_id": checkpoint_id,
                "source_session_id": source_session_id,
                "status": restored_status,
                "autoplay_status": restored_status,
                "hidden_speakers": sorted(session.hidden_speakers),
                "channel": {
                    "acked_seq": channel_data["acked_seq"],
                    "next_seq": channel_data["next_seq"],
                    "max_returned_seq": channel_data["max_returned_seq"],
                    "queued_count": len(events),
                    "events": [dict(e) for e in events],
                },
            }
        )
        return state

    # ------------------------------------------------------------------
    # 自动播放：按真实经过时间推进（单进程内存态后台驱动）
    # ------------------------------------------------------------------

    def _ensure_driver_locked(self) -> None:
        """确保后台驱动线程在运行（调用方持有注册表锁）。

        启动/恢复把状态改为 running 后调用：若驱动尚未启动（或上一个驱动
        已正常退出），现场创建唯一的守护线程。先在线程对象可见后再唤醒，
        避免“驱动已退出、会话却停在 running 无人推进”的丢唤醒窗口。
        tick 间隔配置为 None（仅显式 tick 的确定性模式）时不启动线程。
        """
        if self._autoplay_tick_interval_s is None:
            return
        existing = self._driver_thread
        if existing is not None and existing.is_alive():
            return
        if self._driver_stop.is_set():
            # 本管理器已随 reset_playback 退役；正常路径不可达（reset 会
            # 丢弃整个管理器），此处防御性地不复活旧驱动。
            return
        thread = threading.Thread(
            target=self._driver_loop,
            name="playback-autoplay-driver",
            daemon=True,
        )
        self._driver_thread = thread
        thread.start()
        self._driver_wake.set()

    def _driver_loop(self) -> None:
        """后台驱动：每 50ms 对所有 running 会话尝试推进一次。

        被停止事件或启动/恢复唤醒时立即跑一拍；每拍先取注册表快照再逐个
        进入会话锁，与全部既有会话操作完整串行。会话结束后注册表即无其
        条目，旧的后台任务不可能再推进它。
        """
        while not self._driver_stop.is_set():
            self._driver_wake.wait(self._autoplay_tick_interval_s)
            self._driver_wake.clear()
            if self._driver_stop.is_set():
                return
            with self._registry_lock:
                sessions = list(self._sessions.values())
            for session in sessions:
                try:
                    self._tick_session(session)
                except SessionFinished:
                    # 在取注册表快照与进锁之间会话被结束：按墓碑忽略
                    continue

    def _tick_session(
        self, session: PlaybackSession, now: Optional[float] = None
    ) -> None:
        """对单个会话尝试推进一拍（仅 running 生效）。

        now 可注入单调时钟读数（秒）以便确定性测试；缺省取管理器时钟
        （生产为 time.monotonic()）。目标游标 = 本段起始游标 + 自本段起点
        经过的整毫秒；暂停、阻塞不计时（它们已把状态移出 running），恢复
        从保留游标开新一段。目标不大于当前游标（首拍不足 1ms）时不做任何
        变更。一次推进把整批事件原子入队：共享容量不足时整批转为
        blocked——游标、seq 与队列保持原样，确认释放容量后须显式
        resume，不追赶停顿时间。
        """
        if now is None:
            now = self._clock()
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if session.autoplay_status != AUTOPLAY_RUNNING:
                return
            elapsed_ms = int((now - session.base_monotonic_s) * 1000)
            target = session.base_cursor_ms + elapsed_ms
            if target <= session.cursor_ms:
                return
            try:
                # 与普通手动推进完全相同的窗口、隐藏过滤、排序、seq 与
                # 共享容量语义；整批事件随游标更新一同生效。
                self._advance_locked(session, target, False)
            except ChannelCapacityExhausted:
                # 整批无法入队：游标、seq 与队列均未改变（容量申请先于
                # 一切状态变更）。转为阻塞，保留游标作为恢复后的起点。
                session.autoplay_status = AUTOPLAY_BLOCKED

    def tick_autoplay(self, now: Optional[float] = None) -> None:
        """显式驱动一拍：对所有注册表中的会话尝试推进（确定性测试用）。

        now 为注入的单调时钟读数（秒）；与后台线程的每拍行为完全一致。
        """
        with self._registry_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                self._tick_session(session, now)
            except SessionFinished:
                continue

    def start_autoplay(
        self, session: PlaybackSession, now: Optional[float] = None
    ) -> Dict[str, Any]:
        """启动自动播放：仅限已开启补取通道且从未启动的会话。

        以当前游标为本段起始游标记录单调时钟起点并置为 running，后台驱动
        随即按真实经过时间推进（确定性模式下由显式 tick 驱动）。时钟在
        **会话锁内、状态迁移生效的同一时刻**读取：若同一会话的接纳等操作
        正持锁，本请求在锁外排队等待的时间不属于本段计时——生效时刻才
        开新计时段，随后的推进不会把等锁间隔算进去，也就不会提前入队
        字幕事件。未开启通道抛 AutoplayConflict(channel_missing)；已启动
        （含 running/paused/blocked）抛 AutoplayConflict(already_started)；
        会话已结束抛 SessionFinished。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            if session.channel is None:
                raise AutoplayConflict(
                    "channel_missing", session.autoplay_status
                )
            if session.autoplay_status != AUTOPLAY_IDLE:
                raise AutoplayConflict(
                    "already_started", session.autoplay_status
                )
            # 串行生效点：此刻才读取时钟开新计时段（注入 now 时按注入值），
            # 此前在会话锁外等待的时间不计入。
            if now is None:
                now = self._clock()
            session.autoplay_status = AUTOPLAY_RUNNING
            session.base_cursor_ms = session.cursor_ms
            session.base_monotonic_s = now
            with self._registry_lock:
                self._ensure_driver_locked()
            return session.autoplay_state()

    def pause_autoplay(self, session: PlaybackSession) -> Dict[str, Any]:
        """暂停自动播放：仅限 running；暂停期间不计时。

        不做收尾推进：保留游标停留在后台最后一次成功推进的值（最多一个
        50ms 拍的粒度），状态置为 paused。此后任何已在途的后台 tick 进锁
        后看到非 running 都直接返回，因此暂停返回后旧任务不可能再推进本
        会话；尚未跨过的不到一拍的时间不补发——恢复时以保留游标作为新一段
        起点重新计时，该区间内的事件仍会落在恢复后的推进窗口里正常入队。
        未启动抛 not_started；paused/blocked 抛 pause_only_running。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            status = session.autoplay_status
            if status == AUTOPLAY_IDLE:
                raise AutoplayConflict("not_started", status)
            if status != AUTOPLAY_RUNNING:
                raise AutoplayConflict("pause_only_running", status)
            session.autoplay_status = AUTOPLAY_PAUSED
            return session.autoplay_state()

    def resume_autoplay(
        self, session: PlaybackSession, now: Optional[float] = None
    ) -> Dict[str, Any]:
        """恢复自动播放：仅限 paused 或 blocked；从保留游标重新计时。

        以当前（保留的）游标作为新一段起点重新记录单调时钟，不追赶暂停或
        阻塞期间流逝的时间。与启动相同，时钟在**会话锁内、状态迁移生效的
        同一时刻**读取：本请求在锁外排队等待（例如同会话的接纳正持锁）的
        时间不计入新一段，恢复后的推进不会把等锁间隔算进去。阻塞恢复后若
        容量仍不足，下一拍（注入 now 的显式 tick 或后台线程的下一拍）会
        再次整批失败并回到 blocked（游标、seq、队列不变）。未启动抛
        not_started；running 抛 resume_paused_or_blocked。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            status = session.autoplay_status
            if status == AUTOPLAY_IDLE:
                raise AutoplayConflict("not_started", status)
            if status not in (AUTOPLAY_PAUSED, AUTOPLAY_BLOCKED):
                raise AutoplayConflict("resume_paused_or_blocked", status)
            # 串行生效点：此刻才读取时钟开新计时段（注入 now 时按注入值），
            # 此前在会话锁外等待的时间不计入。
            if now is None:
                now = self._clock()
            session.autoplay_status = AUTOPLAY_RUNNING
            session.base_cursor_ms = session.cursor_ms
            session.base_monotonic_s = now
            with self._registry_lock:
                self._ensure_driver_locked()
            return session.autoplay_state()

    def autoplay_status_of(
        self, session: PlaybackSession
    ) -> Dict[str, Any]:
        """查询自动播放状态与当前游标（未启动返回 idle）。"""
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            return session.autoplay_state()

    def shutdown_driver(self) -> None:
        """停止后台驱动（随 reset_playback 退役管理器时调用）。

        已暂停/阻塞/运行的会话随管理器一同被丢弃；驱动线程在当前一拍
        （每次只短暂持有会话锁）结束后退出，不影响替换后的新管理器。
        """
        self._driver_stop.set()
        self._driver_wake.set()

    def accept(
        self,
        session: PlaybackSession,
        load_record: Callable[[], Optional[SceneRecord]],
    ) -> Dict[str, Any]:
        """接纳场景当前快照：读取场景、校验、重算分轨、替换会话为完整操作。

        load_record 在会话锁内调用，因此读到的是“轮到本次接纳生效时”的
        场景快照；与同一会话的推进/接纳/结束串行。这样并发下迟到的请求
        不会把进入锁之前读到的旧快照回写到会话上。会话已结束抛
        SessionFinished（404）；场景不存在抛 SceneUnavailable；冲突或
        无解抛 AcceptConflict，会话保持不变。
        """
        with session.lock:
            if session.closed:
                raise SessionFinished(session.session_id)
            record = load_record()
            if record is None:
                raise SceneUnavailable(session.scene_id)
            if record.track_count != session.track_count:
                raise AcceptConflict(
                    f"轨道数不得变化：会话为 {session.track_count}，"
                    f"场景当前为 {record.track_count}"
                )

            # 游标前已开始（start_ms <= 游标）的字幕必须原样保留
            started = {
                s["id"]: s
                for s in session.subtitles
                if s["start_ms"] <= session.cursor_ms
            }
            new_by_id = {s["id"]: s for s in record.subtitles}
            for sid, old in started.items():
                new = new_by_id.get(sid)
                if new is None:
                    raise AcceptConflict(f"字幕 {sid!r} 已开始，不得删除")
                if _identity(new) != _identity(old):
                    raise AcceptConflict(
                        f"字幕 {sid!r} 已开始，其 id/speaker/起止时间/"
                        "locked_track 不得修改"
                    )
            # 其余字幕可改删或加入，但都必须在游标之后开始
            for new in record.subtitles:
                if new["start_ms"] <= session.cursor_ms and new["id"] not in started:
                    raise AcceptConflict(
                        f"字幕 {new['id']!r} 的开始时间不晚于游标 "
                        f"{session.cursor_ms}：新增/修改的字幕必须在游标之后开始"
                    )

            # 固定已开始字幕的原分轨，按新快照顺序与最少换轨、并列规则重算
            session_track = {
                s["id"]: t for s, t in zip(session.subtitles, session.tracks)
            }
            subs = [
                Subtitle(
                    id=s["id"],
                    speaker=s["speaker"],
                    start_ms=s["start_ms"],
                    end_ms=s["end_ms"],
                    locked_track=(
                        session_track[s["id"]]
                        if s["id"] in started
                        else s.get("locked_track")
                    ),
                )
                for s in record.subtitles
            ]
            tracks, changes = solve(subs, session.track_count)
            if tracks is None:
                raise AcceptConflict("固定已开始字幕的原分轨后，新快照无解")

            # 成功：整体替换会话快照与源版本（场景本身不变）。
            # hidden_speakers 是仅属于本会话的播放期设置，不在快照内，
            # 接纳后原样保留；active() 与事件推进自然按新字幕的 speaker 过滤。
            session.subtitles = [dict(s) for s in record.subtitles]
            session.tracks = list(tracks)
            session.changes = changes
            session.source_revision = record.revision
            return session.state()

    def finish(self, session_id: str) -> Dict[str, Any]:
        """结束会话：一次完整操作取回最终快照并移除注册表条目。

        完整操作的定序为：先在注册表中查到会话，再进入会话锁，在锁内
        置位 closed 墓碑并取出注册表条目，最后对轮到结束生效时的会话
        状态取快照。与同一会话的推进、事件推进、隐藏切换、接纳共用会话
        锁，因此与它们完整串行：

        - 快照只取自会话内存状态（session.state()），不读取场景存储，
          不会纳入尚未被接纳的场景修订；
        - 已取得会话引用但在锁上等待的在途请求，轮到它时看到 closed，
          一律抛 SessionFinished（404），不会在结束之后再生效一次；
        - 并发结束中第一个通过 closed 检查的请求成功，其余抛
          SessionFinished；注册表已无条目时抛 SessionMissing；
        - 不改动源场景、其他会话或此前返回的快照；不保存结束记录。

        返回的字典沿用创建会话（state()）的全部响应字段。
        """
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionMissing(session_id)
        with session.lock:
            if session.closed:
                raise SessionFinished(session_id)
            session.closed = True
            # 结束会话清除补取通道（通道本就只是会话内存态），并一次性
            # 释放其剩余未确认事件占用的共享容量。closed 检查保证重复或
            # 并发结束只有第一个到达此处：channel 在此被置空，落败方直接
            # 抛 SessionFinished，因此剩余队列恰好释放一次、不会重复释放。
            channel = session.channel
            if channel is not None:
                self._channel_capacity.release(len(channel.events))
                session.channel = None
            with self._registry_lock:
                # 注册表条目即本对象；以 closed 为准，防御性地按 id 移除
                self._sessions.pop(session_id, None)
            # state() 复制字幕与分轨：结束响应不受其后任何变化影响
            return session.state()
