# -*- coding: utf-8 -*-
"""战绩卡相关测试：派生统计 + 版式渲染。

这些测试**不依赖 astrbot**，也不需要网络或牌谱屋 Key：
- 统计部分只喂本地构造的逐局流水；
- 渲染部分用离线构造的 payload 出图，检查尺寸与版式约束。

单独成目录（而不是放进 tests/）是因为 `tests/conftest.py` 在导入期就
`from astrbot.api... import ...`，没装 astrbot 时整目录都收集不了。
这样只装 pillow + pytest 也能验证卡片逻辑：

    pytest tests_card -q
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paifuya import record_analytics as ra  # noqa: E402

pillow = pytest.importorskip("PIL", reason="战绩卡渲染需要 Pillow")
from PIL import Image  # noqa: E402

from paifuya import record_card as rc  # noqa: E402


# ---------------------------------------------------------------- 数据构造

BASE = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)


def make_rows(ranks, gap_minutes=25, duration=22, start=BASE):
    """按给定顺位造逐局流水；局间隔 = gap_minutes（段内连打用）。"""
    rows, t = [], start
    for r in ranks:
        rows.append({
            "start": t,
            "end": t + timedelta(minutes=duration),
            "rank": r,
            "pt": {1: 60, 2: 22, 3: -15, 4: -52}[r],
            "score": 25000 + (2 - r) * 5000,
            "level_id": 10401,
        })
        t = t + timedelta(minutes=gap_minutes)
    return rows


def make_payload(ranks, four=True, detail="full", theme="dark", radar=False):
    rows = make_rows(ranks)
    sessions = ra.find_sessions(rows)
    extra = ra.summarize(rows, sessions, four)
    hours = extra.pop("hours", [])
    return {
        "nickname": "测试玩家",
        "level_name": "雀豪1星",
        "level_id": 10401,
        "level_score": 1340,
        "level_next": 1400,
        "four": four,
        "scope": "四麻 · 最近 %d 局" % len(ranks),
        "seq": list(ranks),
        "level_scores": [1000 + i * 20 for i in range(len(ranks))],
        "rank_changes": [],
        "pt_total": sum(r["pt"] for r in rows),
        "game_rows": rows,
        "sessions": sessions,
        "hours": hours,
        "summary": {"games": len(ranks), "best_streak": 2, "level_delta": 40, **extra},
        "metrics": [],
        "radar": [60, 55, 50, 45, 70] if radar else [],
        "radar_base": [50] * 5,
        "theme": theme,
        "detail": detail,
    }


# ---------------------------------------------------------------- 统计部分

def test_find_sessions_splits_on_gap():
    rows = make_rows([1, 2, 3, 1, 4], gap_minutes=25)
    # 在第 3、4 局之间插入 3 小时间隔
    shift = timedelta(hours=3)
    for r in rows[3:]:
        r["start"] += shift
        r["end"] += shift
    sessions = ra.find_sessions(rows)
    assert len(sessions) == 2
    assert [s["games"] for s in sessions] == [3, 2]
    assert [s["long"] for s in sessions] == [True, False]


def test_find_sessions_empty():
    assert ra.find_sessions([]) == []


def test_stability_small_sample_has_no_label():
    """样本 <5 局只报 sd，不给档位——2~3 局的波动全是运气。"""
    out = ra.stability([1, 4], four=True)
    assert out["label"] == "样本少"
    assert out["sd"] is not None


def test_stability_labels_monotonic():
    steady = ra.stability([2] * 10, four=True)
    wild = ra.stability([1, 4] * 5, four=True)
    assert steady["sd"] < wild["sd"]
    assert steady["label"] in {"很稳", "较稳"}
    assert wild["label"] == "波动大"


def test_three_player_last_rank_is_three():
    extra = ra.summarize(make_rows([1, 3, 3, 2]), ra.find_sessions(make_rows([1, 3, 3, 2])), False)
    assert extra["last_rate"] == pytest.approx(0.5)


def test_hour_buckets_cover_all_rows():
    rows = make_rows([1, 2, 3, 4] * 3)
    extra = ra.summarize(rows, ra.find_sessions(rows), True)
    assert sum(b["games"] for b in extra["hours"]) == len(rows)


def test_summarize_empty_is_empty():
    assert ra.summarize([], [], True) == {}


def test_point_curve_payload_baseline():
    rows = make_rows([1, 2])
    p = ra.point_curve_payload(rows)
    assert p["baseline"] == 25000
    assert len(p["points"]) == 2
    assert p["avg"] == round(sum(p["points"]) / 2)


# ---------------------------------------------------------------- 渲染部分

def _render(payload):
    return Image.open(__import__("io").BytesIO(rc.render_record(payload)))


def test_render_returns_png_of_expected_width():
    img = _render(make_payload([1, 2, 3, 4] * 5))
    assert img.width == rc.W
    assert img.height > 400


def test_compact_is_shorter_than_full():
    ranks = [1, 2, 3, 4] * 8
    full = _render(make_payload(ranks, detail="full")).height
    compact = _render(make_payload(ranks, detail="compact")).height
    assert compact < full


def test_sparse_games_render_shorter_than_dense():
    """2 局不该和 40 局一样高（旧版写死高度，2 局也出 1166px）。"""
    sparse = _render(make_payload([1, 2])).height
    dense = _render(make_payload([1, 2, 3, 4] * 10)).height
    assert sparse < dense - 100


def test_light_theme_renders():
    img = _render(make_payload([1, 2, 3, 4] * 3, theme="light"))
    assert img.width == rc.W


def test_three_player_payload_renders():
    img = _render(make_payload([1, 2, 3] * 6, four=False))
    assert img.width == rc.W


def test_radar_payload_renders():
    img = _render(make_payload([1, 2, 3, 4] * 6, radar=True))
    assert img.width == rc.W


def test_pt_bar_small_span_does_not_crash():
    """PT 跨度 < 40 时曾走浮点分支，f"{hi:+d}" 抛 ValueError。"""
    payload = make_payload([1, 1, 1, 1])
    for r in payload["game_rows"]:
        r["pt"] = 5            # 全是小数，跨度远小于 40
    img = _render(payload)
    assert img.width == rc.W


def test_single_game_does_not_crash():
    img = _render(make_payload([2]))
    assert img.width == rc.W


def test_chart_scale_is_monotonic_and_capped():
    assert rc._chart_scale(2) < rc._chart_scale(10) <= rc._chart_scale(20)
    assert rc._chart_scale(500) == rc._chart_scale(20)


def test_split_width_columns_do_not_overlap():
    left, right = rc._split_w(rc.W)
    assert left + right + 2 * rc.PAD + rc.GAP == rc.W
    assert left > right > 0