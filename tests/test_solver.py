"""核心求解器测试（仅依赖标准库，可直接用系统 Python 运行）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.solver import Subtitle, solve, validate  # noqa: E402


def S(id, speaker, start, end, locked=None):
    return Subtitle(id=id, speaker=speaker, start_ms=start, end_ms=end,
                    locked_track=locked)


def run(subs, tracks):
    return solve(subs, tracks)


def test_single_subtitle():
    assignment, changes = run([S("a", "A", 0, 100)], 2)
    assert assignment == [1]
    assert changes == 0


def test_overlap_uses_different_tracks_and_lex_min():
    # 两个最优解 [1,2,1] 与 [2,1,2] 换轨均为 0，取字典序最小
    subs = [S(1, "A", 0, 1000), S(2, "B", 500, 1500), S(3, "A", 1000, 2000)]
    assignment, changes = run(subs, 2)
    assert assignment == [1, 2, 1]
    assert changes == 0


def test_touching_endpoints_share_track():
    subs = [S("a", "A", 0, 100), S("b", "B", 100, 200)]
    assignment, changes = run(subs, 2)
    assert assignment == [1, 1]
    assert changes == 0


def test_three_clique_two_tracks_infeasible():
    subs = [S(1, "A", 0, 10), S(2, "B", 1, 11), S(3, "C", 2, 12)]
    assert run(subs, 2) == (None, 0)


def test_three_clique_three_tracks_ok():
    subs = [S(1, "A", 0, 10), S(2, "B", 1, 11), S(3, "C", 2, 12)]
    assignment, changes = run(subs, 3)
    assert assignment == [1, 2, 3]
    assert changes == 0


def test_locked_conflict_infeasible():
    subs = [S("a", "A", 0, 100, locked=1), S("b", "B", 50, 150, locked=1)]
    assert run(subs, 2) == (None, 0)


def test_locked_tracks_forced_and_changes_counted():
    subs = [S("a", "A", 0, 100, locked=2), S("b", "A", 100, 200, locked=1)]
    assignment, changes = run(subs, 2)
    assert assignment == [2, 1]
    assert changes == 1


def test_changes_follow_time_order_not_input_order():
    # 同一说话人 A 的时间顺序为 a1 -> a2 -> a3，输入顺序为 a1, a3, a2。
    # 阻挡字幕 b(锁2)、c(锁1) 迫使 a2=1、a3=2：
    # 时间相邻换轨：a1(1)->a2(1)->a3(2) 共 1 次；
    # 若错误地按输入相邻计：a1->a3、a3->a2 共 2 次。
    subs = [
        S("a1", "A", 0, 10, locked=1),
        S("a3", "A", 40, 50),
        S("a2", "A", 20, 30),
        S("b", "B", 22, 28, locked=2),
        S("c", "C", 42, 48, locked=1),
    ]
    assignment, changes = run(subs, 2)
    assert assignment == [1, 2, 1, 2, 1]
    assert changes == 1


def test_lex_break_when_change_tied():
    # A 与 B 交替重叠：验证边界相接可复用，且整体序列字典序最小
    subs = [
        S(0, "A", 0, 10),       # 与后面 B 重叠
        S(1, "B", 5, 15),
        S(2, "A", 10, 20),
        S(3, "B", 15, 25),      # 与 A s2 重叠
        S(4, "A", 20, 30),
    ]
    assignment, changes = run(subs, 2)
    # 所有 A 都压在轨道 1（B 在轨道 2），A 时间相邻零换轨
    assert assignment == [1, 2, 1, 2, 1]
    assert changes == 0


def test_validate_duplicate_ids():
    subs = [S("x", "A", 0, 10), S("x", "B", 0, 10)]
    assert validate(subs, 2) is not None


def test_validate_same_speaker_overlap():
    subs = [S("a", "A", 0, 100), S("b", "A", 50, 150)]
    assert validate(subs, 2) is not None


def test_validate_locked_track_out_of_range():
    subs = [S("a", "A", 0, 100, locked=3)]
    assert validate(subs, 2) is not None


def test_validate_ok():
    subs = [S("a", "A", 0, 100), S("b", "B", 0, 100)]
    assert validate(subs, 2) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
