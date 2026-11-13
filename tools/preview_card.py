"""本地预览卡片：不连机器人、不请求网络，用示例数据直接出图。

用法（在插件根目录执行）::

    python tools/preview_card.py [dark|light] [输出文件]       # 战绩走势卡
    python tools/preview_card.py ai [dark|light] [输出]        # AI 锐评卡
    python tools/preview_card.py all [输出目录]                 # 两张都出

用于快速检查版式改动（字号、间距、颜色、跨段断线、锐评折行等），
改完 `paifuya/record_card.py` 再跑一次即可看到结果。
"""
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paifuya import record_card  # noqa: E402
from paifuya import record_analytics  # noqa: E402


def sample_payload(theme: str = "dark", detail: str = "full") -> dict:
    rng = random.Random(20261113)
    seq, scores, cur = [], [], 1400
    for i in range(40):
        rank = rng.choices([1, 2, 3, 4], weights=[30, 26, 25, 19])[0]
        seq.append(rank)
        cur += {1: 62, 2: 24, 3: -16, 4: -54}[rank] + rng.randint(-8, 8)
        scores.append(cur)

    # 造逐局流水：每 5 局成一段，段间空 3 小时，便于看「连打时段」底色
    rows = []
    base = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
    for i, rank in enumerate(seq):
        start = base + timedelta(minutes=(i // 5) * 300 + (i % 5) * 28)
        pt = {1: 62, 2: 24, 3: -16, 4: -54}[rank] + rng.randint(-8, 8)
        rows.append({"start": start, "end": start + timedelta(minutes=22), "rank": rank,
                     "pt": pt, "score": 25000 + (seq[i] - 2) * -5200 + rng.randint(-2500, 2500),
                     "level_id": 10401 if i < 23 else 10402})
    sessions = record_analytics.find_sessions(rows)
    extra = record_analytics.summarize(rows, sessions, True)

    return {
        "nickname": "二阶堂美树",
        "level_name": "雀豪1星",
        "level_id": 10401,
        "level_score": 1340,
        "level_next": 1400,
        "four": True,
        "scope": "四麻 · 最近 40 局",
        "seq": seq,
        "level_scores": scores,
        "rank_changes": [{"index": 23, "level_id": 10402, "name": "雀豪2星"}],
        "pt_total": 210,
        "game_rows": rows,
        "sessions": sessions,
        "hours": extra.pop("hours", []),
        "summary": {
            "games": len(seq),
            "avg_rank": round(sum(seq) / len(seq), 2),
            "win_rate": sum(1 for r in seq if r == 1) / len(seq),
            "top2_rate": sum(1 for r in seq if r <= 2) / len(seq),
            "last_rate": sum(1 for r in seq if r == 4) / len(seq),
            "best_streak": 3,
            "level_delta": 210,
            **extra,
        },
        "detail": detail,
        "metrics": [
            {"label": "和牌率", "value": "22.6%", "avg": "21.9%", "better": True},
            {"label": "放铳率", "value": "12.2%", "avg": "12.9%", "better": True},
            {"label": "副露率", "value": "36.0%", "avg": "31.8%", "better": None},
            {"label": "立直率", "value": "20.0%", "avg": "18.7%", "better": None},
            {"label": "平均打点", "value": "6,517", "avg": "6,558", "better": False},
            {"label": "平均铳点", "value": "5,067", "avg": "5,544", "better": True},
            {"label": "净打点效率", "value": "852", "avg": "717", "better": True},
            {"label": "和了巡数", "value": "12.30", "avg": "12.20", "better": False},
            {"label": "立直巡目", "value": "9.60", "avg": "9.50", "better": False},
            {"label": "先制率", "value": "83.7%", "avg": "80.6%", "better": True},
            {"label": "立直后和牌率", "value": "49.8%", "avg": "50.3%", "better": False},
            {"label": "副露后和牌率", "value": "27.0%", "avg": "30.5%", "better": False},
        ],
        "metrics_scope": "雀豪1星 玉南 段位均值",
        "radar": [62.0, 55.0, 61.0, 48.0, 44.0],
        "radar_base": "雀豪1星 玉南",
        "theme": theme,
    }


AI_TEXT = (
    "喵～最近的段位分走势很漂亮，净赚 210 分，说明整体牌风是能上分的。"
    "但看顺位曲线能发现一个明显问题：赢的时候常常是一波连对，输的时候容易连着四位，"
    "波动偏大。\n"
    "和牌率 22.6% 略高于同段位均值，进攻方向没问题；放铳率 12.2% 也低于均值，"
    "说明防守基本功是有的。真正拖后腿的是平均打点（6,517 vs 均值 6,558）"
    "和副露后和牌率（27.0% vs 均值 30.5%）——副露判断偏多但收益不够。\n"
    "建议：1）中盘鸣牌前先算一下役种与打点，别为了快而牺牲打点；"
    "2）立直巡目 9.60 略慢于均值，听牌速度可以再提一提；"
    "3）领先局减少无谓副露，把四位率压下来。按这个调整，上分会更稳喵。"
)


def sample_comment(theme: str = "dark") -> dict:
    return {
        "nickname": "二阶堂美树",
        "level_name": "雀豪1星",
        "level_id": 10401,
        "scope": "四麻 · 最近 40 局",
        "chips": ["平均顺位 2.28", "一位率 30%", "四位率 19%",
                  "最长连一位 3", "段位分 +210"],
        "text": AI_TEXT,
        "model": "gpt-4o-mini",
        "theme": theme,
    }


def main() -> None:
    if not record_card.available():
        raise SystemExit("未安装 Pillow，无法渲染（pip install pillow）")

    args = sys.argv[1:]
    if args and args[0] == "all":
        out_dir = Path(args[1]) if len(args) > 1 else ROOT
        out_dir.mkdir(parents=True, exist_ok=True)
        for theme in ("dark", "light"):
            for detail in ("full", "compact"):
                name = f"preview_record_{theme}" + ("" if detail == "full" else "_compact")
                a = out_dir / f"{name}.png"
                a.write_bytes(record_card.render_record(sample_payload(theme, detail)))
                print(f"已输出 {a.name} ({a.stat().st_size // 1024} KB)")
            b = out_dir / f"preview_ai_{theme}.png"
            b.write_bytes(record_card.render_comment(sample_comment(theme)))
            print(f"已输出 {b.name} ({b.stat().st_size // 1024} KB)")
        return

    kind, rest = "record", args
    detail = "full"
    if args and args[0] == "ai":
        kind, rest = "comment", args[1:]
    elif args and args[0] == "compact":
        detail, rest = "compact", args[1:]
    theme = rest[0] if len(rest) > 0 else "dark"
    suffix = "" if kind == "comment" or detail == "full" else "_compact"
    default = ROOT / (f"preview_record_{theme}{suffix}.png" if kind == "record"
                      else f"preview_ai_{theme}.png")
    out = Path(rest[1]) if len(rest) > 1 else default
    if kind == "record":
        out.write_bytes(record_card.render_record(sample_payload(theme, detail)))
    else:
        out.write_bytes(record_card.render_comment(sample_comment(theme)))
    print(f"已输出 {out}（{out.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()