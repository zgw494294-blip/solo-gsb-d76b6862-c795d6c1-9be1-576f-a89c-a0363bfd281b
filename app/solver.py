"""对白字幕分轨核心模型与求解器（无第三方依赖，可独立运行）。"""
from __future__ import annotations

from typing import List, Optional, Tuple, Union

SubtitleId = Union[str, int]


class Subtitle:
    """一条字幕。id 保留请求中的原始类型。"""

    __slots__ = ("id", "speaker", "start_ms", "end_ms", "locked_track")

    def __init__(
        self,
        id: SubtitleId,
        speaker: str,
        start_ms: int,
        end_ms: int,
        locked_track: Optional[int] = None,
    ) -> None:
        self.id = id
        self.speaker = speaker
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.locked_track = locked_track


def validate(subs: List[Subtitle], track_count: int) -> Optional[str]:
    """业务级请求校验。

    轨道数范围、字幕数量、字段类型与非负等规则由 Pydantic 模型负责；
    本函数补充无法用声明式注解表达的跨条目约束。
    返回 None 表示合法，否则返回错误描述。
    """
    seen_ids = set()
    for s in subs:
        sid = s.id
        if isinstance(sid, str) and sid == "":
            return "字幕 id 不能为空字符串"
        if sid in seen_ids:
            return f"字幕 id 重复: {sid!r}"
        seen_ids.add(sid)
        if s.locked_track is not None and s.locked_track > track_count:
            return f"字幕 {sid!r} 锁定轨道 {s.locked_track} 超出可用轨道数 {track_count}"

    # 同一说话人的字幕不得重叠（左闭右开，端点相接不算重叠）
    by_speaker: dict[str, List[Subtitle]] = {}
    for s in subs:
        by_speaker.setdefault(s.speaker, []).append(s)
    for group in by_speaker.values():
        group.sort(key=lambda x: (x.start_ms, x.end_ms))
        for a, b in zip(group, group[1:]):
            if a.end_ms > b.start_ms:
                return f"说话人 {a.speaker!r} 的字幕时间重叠: {a.id!r} 与 {b.id!r}"
    return None


def solve(
    subs: List[Subtitle], track_count: int
) -> Tuple[Optional[List[int]], int]:
    """为全部字幕选轨。

    目标：同一说话人按时间相邻的两条字幕，换轨次数总和最小；
    并列时取“按输入顺序排列的轨道号序列”字典序最小者。
    返回 (轨道列表, 换轨总数)；无解时返回 (None, 0)。
    """
    n = len(subs)

    # overlap[i] 记录与第 i 条字幕时间重叠的、输入顺序更靠前的字幕集合。
    overlap: List[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        si = subs[i]
        for j in range(i):
            sj = subs[j]
            # 左闭右开：[start, end)，端点相接允许同轨
            if si.start_ms < sj.end_ms and sj.start_ms < si.end_ms:
                overlap[i].add(j)

    # peers[i]：与第 i 条字幕属于同一说话人、且在该说话人时间轴上相邻的条目。
    # 相邻关系按时间排序后定义，与输入顺序无关。
    peers: List[set[int]] = [set() for _ in range(n)]
    by_speaker: dict[str, List[int]] = {}
    for i, s in enumerate(subs):
        by_speaker.setdefault(s.speaker, []).append(i)
    for indices in by_speaker.values():
        chain = sorted(indices, key=lambda idx: (subs[idx].start_ms, subs[idx].end_ms))
        for a, b in zip(chain, chain[1:]):
            peers[a].add(b)
            peers[b].add(a)

    assignment = [0] * n
    best: Optional[List[int]] = None
    best_cost = 0

    def dfs(i: int, cost: int) -> None:
        nonlocal best, best_cost
        # 已找到最优解后，代价不可能再下降：剪掉不优于当前最优的分支
        if best is not None and cost >= best_cost:
            return
        if i == n:
            best = assignment.copy()
            best_cost = cost
            return

        occupied = {assignment[j] for j in overlap[i]}
        locked = subs[i].locked_track
        # 按轨道号从小到大尝试，保证最先到达的最优解即字典序最小
        candidates = (locked,) if locked is not None else range(1, track_count + 1)
        for track in candidates:
            if track in occupied:
                continue
            if locked is not None and locked > track_count:
                continue
            assignment[i] = track
            # 仅为“两端都已落位”的同说话人相邻对计一次费
            extra = sum(
                1
                for j in peers[i]
                if j < i and assignment[j] != track
            )
            dfs(i + 1, cost + extra)
        assignment[i] = 0

    dfs(0, 0)
    if best is None:
        return None, 0
    return best, best_cost
