# -*- coding: utf-8 -*-
"""战绩的派生统计（纯函数，无 astrbot / Pillow 依赖）。

把「逐局记录」压成卡片需要的几组结论，全部可从牌谱屋已有字段算出：

- **逐局流水**：顺位、PT、素点（终局点数）、起止时间；
- **连打分段**：相邻两局间隔超过阈值即视为新的一段（牌谱屋没有「房间/席位」概念，
  只能按时间间隔切分，这也是各家战绩工具的通行做法）；
- **节奏**：日均局数、相邻局中位间隔、最近对战、按小时段的局数与均顺；
- **稳定度**：顺位标准差的归一化值（自建指标，只用于横向比较）。

约定：本模块只做计算，不格式化字符串、不画图；数值口径与 `query_majsoul_record`
的其它部分保持一致（顺位＝同局终局点数排名）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median

SESSION_GAP_MINUTES = 30      # 相邻两局间隔超过它就算「另开一段」
LONG_SESSION_MIN_GAMES = 3    # 局数 ≥ 它的段计为「连打」，其余计为「散打」

# 牌谱屋 rank_rates 的顺序就是一位、二位、三位、四位
_HOUR_BUCKETS = ((0, 6, "深夜"), (6, 12, "上午"), (12, 18, "下午"), (18, 24, "晚间"))


def game_rows(records: list, player_id: int) -> list[dict]:
    """逐局流水（旧→新）。`records` 需已按时间升序。"""
    rows: list[dict] = []
    for rec in records:
        focus = next((p for p in rec.players if p.id == player_id), None)
        if focus is None:
            continue
        ordered = sorted(rec.players, key=lambda p: -int(p.score))
        rank = next((i + 1 for i, p in enumerate(ordered) if p.id == player_id), 0)
        rows.append({
            "start": rec.start_time,
            "end": rec.end_time,
            "rank": rank,
            "pt": int(focus.pt),
            "score": int(focus.score),
            "level_id": int(focus.rank.code),
        })
    return rows


def find_sessions(rows: list[dict],
                  gap_minutes: float = SESSION_GAP_MINUTES) -> list[dict]:
    """按时间间隔把逐局切成若干「连打段」。"""
    if not rows:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(rows)):
        gap = (rows[i]["start"] - rows[i - 1]["end"]).total_seconds() / 60
        if gap > gap_minutes:
            spans.append((start, i - 1))
            start = i
    spans.append((start, len(rows) - 1))

    out: list[dict] = []
    for a, b in spans:
        ranks = [rows[i]["rank"] for i in range(a, b + 1)]
        games = b - a + 1
        out.append({
            "start_index": a,
            "end_index": b,
            "games": games,
            "hours": round((rows[b]["end"] - rows[a]["start"]).total_seconds() / 3600, 1),
            "avg_rank": round(sum(ranks) / games, 2),
            "pt": sum(rows[i]["pt"] for i in range(a, b + 1)),
            "started": rows[a]["start"],
            "long": games >= LONG_SESSION_MIN_GAMES,
        })
    return out


def _hour_buckets(rows: list[dict]) -> list[dict]:
    """按本地时间的时段统计局数与均顺。"""
    buckets = [{"name": name, "games": 0, "ranks": []} for _s, _e, name in _HOUR_BUCKETS]
    for r in rows:
        try:
            hour = r["start"].astimezone().hour          # 转成机器人所在时区
        except Exception:                                # noqa: BLE001
            hour = r["start"].hour
        for idx, (lo, hi, _name) in enumerate(_HOUR_BUCKETS):
            if lo <= hour < hi:
                buckets[idx]["games"] += 1
                buckets[idx]["ranks"].append(r["rank"])
                break
    out = []
    for b in buckets:
        n = b["games"]
        out.append({"name": b["name"], "games": n,
                    "avg_rank": round(sum(b["ranks"]) / n, 2) if n else None})
    return out


def stability(ranks: list[int], four: bool) -> dict:
    """顺位波动：给出标准差与定性档位。

    刻意**不**折算成 0~100 分——小样本下顺位标准差可以略超「完全随机」的理论值
    （四麻 ≈1.12、三麻 ≈0.82），归一化会把这类样本压成 0 分，看起来像 bug。
    直接报 sd + 档位更好解释，也方便和参考图的「均顺」并排阅读。

    样本 <5 局时不给档位：2~3 局的 sd 完全由运气决定，标「很稳/波动大」都是误导。
    """
    n = len(ranks)
    if not n:
        return {"sd": None, "label": "", "uniform": None}
    mean = sum(ranks) / n
    sd = (sum((r - mean) ** 2 for r in ranks) / n) ** 0.5
    uniform = 1.12 if four else 0.82          # 完全随机时的理论标准差
    if n < 5:
        return {"sd": round(sd, 2), "label": "样本少", "uniform": uniform}
    ratio = sd / uniform
    if ratio <= 0.72:
        label = "很稳"
    elif ratio <= 0.88:
        label = "较稳"
    elif ratio <= 1.02:
        label = "一般"
    else:
        label = "波动大"
    return {"sd": round(sd, 2), "label": label, "uniform": uniform}


def summarize(rows: list[dict], sessions: list[dict], four: bool) -> dict:
    """一次算齐卡片要的派生数据。"""
    n = len(rows)
    if not n:
        return {}
    ranks = [r["rank"] for r in rows]
    scores = [r["score"] for r in rows]
    last_rank = 4 if four else 3

    # 最长连一位 / 最长连四位
    best_win = worst = cur_win = cur_worst = 0
    for r in ranks:
        cur_win = cur_win + 1 if r == 1 else 0
        cur_worst = cur_worst + 1 if r == last_rank else 0
        best_win = max(best_win, cur_win)
        worst = max(worst, cur_worst)

    gaps = [(rows[i]["start"] - rows[i - 1]["end"]).total_seconds() / 60
            for i in range(1, n)]
    gaps = [g for g in gaps if g >= 0]

    span_days = max(1.0, (rows[-1]["end"] - rows[0]["start"]).total_seconds() / 86400)
    last_end = rows[-1]["end"]
    idle_days = max(0, (datetime.now(timezone.utc) - last_end).days)

    long_games = sum(s["games"] for s in sessions if s["long"])
    long_ranks = [rows[i]["rank"] for s in sessions if s["long"]
                  for i in range(s["start_index"], s["end_index"] + 1)]
    long_pt = sum(s["pt"] for s in sessions if s["long"])
    casual_ranks = [rows[i]["rank"] for s in sessions if not s["long"]
                    for i in range(s["start_index"], s["end_index"] + 1)]
    casual_pt = sum(s["pt"] for s in sessions if not s["long"])

    top_session = max(sessions, key=lambda s: (s["games"], s["hours"])) if sessions else None

    return {
        "avg_rank": round(sum(ranks) / n, 2),
        "win_rate": round(ranks.count(1) / n, 4),
        "top2_rate": round(sum(1 for r in ranks if r <= 2) / n, 4),
        "last_rate": round(ranks.count(last_rank) / n, 4),
        "best_streak": best_win,
        "worst_streak": worst,
        "avg_score": round(sum(scores) / n),
        "max_score": max(scores),
        "min_score": min(scores),
        "scores": scores,
        "games": n,
        "per_day": round(n / span_days, 1),
        "gap_median": round(median(gaps), 1) if gaps else None,
        "idle_days": idle_days,
        "span_days": round(span_days, 1),
        "hours": _hour_buckets(rows),
        "stability": stability(ranks, four),
        "session_count": len(sessions),
        "long": {
            "games": long_games,
            "avg_rank": round(sum(long_ranks) / len(long_ranks), 2) if long_ranks else None,
            "pt": long_pt,
        },
        "casual": {
            "games": len(casual_ranks),
            "avg_rank": round(sum(casual_ranks) / len(casual_ranks), 2) if casual_ranks else None,
            "pt": casual_pt,
        },
        "top_session": top_session,
    }


def point_curve_payload(rows: list[dict]) -> dict:
    """素点曲线的绘制数据（基准线 25000）。"""
    scores = [r["score"] for r in rows]
    return {
        "points": scores,
        "baseline": 25000,
        "avg": round(sum(scores) / len(scores)) if scores else 0,
    }