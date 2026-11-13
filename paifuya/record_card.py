# -*- coding: utf-8 -*-
"""雀魂战绩走势卡（纯 Pillow 渲染，无 astrbot 依赖）。

设计目标：把「一局一局的牌谱屋记录」画成一张能一眼看懂走势的图卡。
版式参考 majsoul-tracker / pecuria 的高密度战绩卡——**左栏走势、右栏结论**，
横向 940px 双栏，比单栏 640px 少滚动约三分之一。

左栏（走势）：段位分曲线、素点曲线、逐局 PT 柱、顺位走势；
右栏（结论）：全局总结 KPI、顺位分布、连打 vs 散打、最长连打段、打牌时段；
通栏（对比）：摘要行、关键指标对比、牌风雷达 + 详细档案。

约定：
- 本模块**只做绘制**，所有数值（含「谁更好」的判断、雷达归一化）都由调用方算好；
- 渲染失败一律抛异常，由调用方回退纯文本；
- 未安装 Pillow 时 :func:`available` 返回 False；
- 牌谱屋不提供逐手 AI 复盘数据，故**没有** Rating / AI 一致率 / 恶手率这类指标，
  这里用素点、逐局 PT、连打分段等真实可算的量替代，不编造数值。
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path

try:  # Pillow 是硬依赖；缺失时由调用方降级
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except Exception:  # noqa: BLE001
    HAS_PIL = False

W = 940                    # 战绩卡宽度（双栏；手机端缩放后仍可读）
W_NARROW = 640             # AI 锐评卡等纯文本卡沿用窄版
H_MAX = 3200               # 先画后裁，避免估算高度出错把内容裁掉
PAD = 22                   # 页面左右留白
GAP = 16                   # 双栏间距
COL_GAP = 10               # 面板之间竖向间距


def _split_w(w: int) -> tuple[int, int]:
    """通栏宽度 → (左栏宽, 右栏宽)。左栏放图表（需要更宽），右栏放结论。"""
    inner = w - 2 * PAD - GAP
    left = int(inner * 0.635)
    return left, inner - left


# 图表基准高度 / 最小高度 / 拉伸上限：局数少时压矮，局数多时靠右栏拉高
_CHART_H = {"score": 132, "point": 118, "pt": 108, "rank": 132}
_CHART_MIN = {"score": 104, "point": 96, "pt": 88, "rank": 104}
_CHART_CAP = {"score": 190, "point": 175, "pt": 160, "rank": 190}


def _chart_scale(n: int) -> float:
    """局数越少，折线图越没必要画那么高——2 个点撑满 132px 只是浪费版面。"""
    if n >= 20:
        return 1.0
    if n >= 10:
        return 0.86
    if n >= 5:
        return 0.74
    return 0.64


def _fit_heights(base: list[int], cap: list[int], target: int) -> list[int]:
    """把高度按上限约束分摊到 target（总高），用于填平左右栏高差。"""
    heights = list(base)
    leftover = target - sum(base)
    for _ in range(6):
        if leftover <= 0:
            break
        active = [i for i in range(len(heights)) if heights[i] < cap[i]]
        if not active:
            break
        share = leftover // len(active)
        if share <= 0:                      # 余量不够整分，逐个 +1
            for i in active:
                if leftover <= 0:
                    break
                heights[i] += 1
                leftover -= 1
            break
        for i in active:
            add = min(share, cap[i] - heights[i])
            heights[i] += add
            leftover -= add
    return heights


_BUNDLED_FONT = Path(__file__).resolve().parent / "NotoSansCJK-Regular.otf"
_LEVEL_DIR = Path(__file__).resolve().parent / "assets" / "levels"

THEMES = {
    "dark": {
        "bg": (14, 24, 19), "panel": (24, 38, 30), "panel2": (32, 49, 38),
        "bar_bg": (10, 18, 14), "border": (52, 82, 64), "grid": (44, 64, 52),
        "text": (237, 230, 214), "sub": (150, 172, 156),
        "gold": (227, 195, 107), "gold_dim": (150, 126, 66),
        "red": (226, 96, 79), "green": (95, 191, 127), "blue": (91, 155, 224),
        "silver": (196, 203, 210), "orange": (214, 148, 78),
    },
    "light": {
        "bg": (243, 245, 248), "panel": (255, 255, 255), "panel2": (246, 248, 251),
        "bar_bg": (228, 233, 238), "border": (206, 213, 222), "grid": (225, 230, 236),
        "text": (38, 46, 56), "sub": (110, 122, 136),
        "gold": (176, 132, 26), "gold_dim": (198, 168, 96),
        "red": (206, 70, 60), "green": (32, 138, 84), "blue": (46, 108, 210),
        "silver": (130, 140, 152), "orange": (200, 124, 46),
    },
}

_BOLD_MIN_SIZE = 13       # 小于这个字号不做描边加粗（笔画会粘连）

_FONTS: dict = {}
_LEVEL_ICONS: dict = {}

MAJOR_NAMES = {1: "初心", 2: "雀士", 3: "雀杰", 4: "雀豪", 5: "雀圣", 6: "魂天", 7: "魂天"}
MAJOR_SHORT = {1: "心", 2: "士", 3: "杰", 4: "豪", 5: "圣", 6: "魂", 7: "魂"}
MAJOR_COLORS = {
    1: (150, 158, 168), 2: (95, 191, 127), 3: (91, 155, 224),
    4: (176, 124, 226), 5: (227, 195, 107), 6: (226, 96, 79), 7: (226, 96, 79),
}

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u2B00-\u2BFF]")


def available() -> bool:
    """Pillow 是否可用。"""
    return HAS_PIL


# ---------------------------------------------------------------- 基础工具

def strip_emoji(s) -> str:
    """去掉 emoji（中文字体没有彩色 emoji 字形，画出来是方框）。"""
    return EMOJI_RE.sub("", str(s or "")).strip()


_MD_RE = re.compile(r"(\*\*|__|~~|`)")
_MD_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)


def strip_markdown(s) -> str:
    """去掉模型常见的 Markdown 标记——提示词里禁止了，但模型未必守规矩。"""
    s = _MD_HEAD_RE.sub("", str(s or ""))
    s = _MD_RE.sub("", s)
    s = re.sub(r"^\s*[-*+]\s+", "· ", s, flags=re.M)      # 列表符换成中式圆点
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


def _font(size: int):
    key = int(size)
    if key not in _FONTS:
        f = None
        if _BUNDLED_FONT.is_file():
            try:
                f = ImageFont.truetype(str(_BUNDLED_FONT), key)
            except Exception:  # noqa: BLE001
                f = None
        _FONTS[key] = f or ImageFont.load_default()
    return _FONTS[key]


def _txt(d, xy, s, size, th, color=None, bold=False, anchor=None):
    """写文字；`bold` 用同色描边模拟加粗（内置字体无粗体字重）。

    注意：中文笔画本身就细，12px 以下再加 1px 描边会让笔画粘连成黑块
    （「连打」两个字直接糊成一坨）。小字号下改用颜色/字重区分，不加描边。
    """
    kw = {"font": _font(size), "fill": color or th["text"], "anchor": anchor}
    if bold and int(size) >= _BOLD_MIN_SIZE:
        kw["stroke_width"] = 1
        kw["stroke_fill"] = color or th["text"]
    d.text(xy, str(s), **kw)


def _tw(d, s, size) -> float:
    return d.textlength(str(s), font=_font(size))


def _ellipsize(d, s, size, max_w) -> str:
    s = str(s or "")
    if _tw(d, s, size) <= max_w:
        return s
    while s and _tw(d, s + "…", size) > max_w:
        s = s[:-1]
    return s + "…"


def _wrap(d, s, size, max_w) -> list[str]:
    """按像素宽度折行（中文逐字、西文尽量整词），空行保留为段落间距。"""
    out: list[str] = []
    for para in str(s or "").split("\n"):
        para = para.rstrip()
        if not para.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        cur = ""
        for ch in para:
            if _tw(d, cur + ch, size) <= max_w:
                cur += ch
                continue
            if ch.isascii() and " " in cur:          # 西文按空格回退，别把单词劈开
                head, _, tail = cur.rpartition(" ")
                if head:
                    out.append(head)
                    cur = (tail + ch).lstrip()
                    continue
            out.append(cur)
            cur = "" if ch == " " else ch
        if cur:
            out.append(cur)
    while out and out[-1] == "":
        out.pop()
    return out


def _blend(c1, c2, t: float):
    """把 c1 按比例 t 混合到 c2（t=0 全 c2，t=1 全 c1）——代替透明度叠色。"""
    t = max(0.0, min(1.0, float(t)))
    return tuple(int(round(a * t + b * (1 - t))) for a, b in zip(c1, c2))


def _panel(d, x, y, w, h, th, fill=None, outline=None, r=10):
    d.rounded_rectangle([x, y, x + w, y + h], radius=r,
                        fill=fill or th["panel"], outline=outline or th["border"], width=1)


def _progress(d, x, y, w, h, pct, th, color=None):
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2,
                        fill=th["bar_bg"], outline=th["border"], width=1)
    pct = max(0.0, min(1.0, float(pct or 0)))
    if pct > 0:
        fill_w = max(h, int(w * pct))
        d.rounded_rectangle([x + 1, y + 1, min(x + w - 1, x + fill_w), y + h - 1],
                            radius=max(1, (h - 2) // 2), fill=color or th["gold"])


def _star(d, cx, cy, r, fill):
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=fill)


def parse_level_id(level_id) -> tuple:
    """段位 id → `(麻种, 大段位, 星)`；非法返回 `(0, 0, 0)`。"""
    try:
        lid = int(level_id or 0)
    except (TypeError, ValueError):
        return 0, 0, 0
    if lid <= 0:
        return 0, 0, 0
    cat, major, star = lid // 10000, (lid // 100) % 100, lid % 100
    if not (1 <= major <= 7) or star < 1:
        return 0, 0, 0
    return cat, major, star


def _level_icon(cat: int, major: int, size: int):
    """取段位图标（先试麻种专用版 `sima_*` / `sanma_*`，再试通用版）；无素材返回 None。"""
    name = {1: "fish", 2: "queshi", 3: "quejie", 4: "quehao", 5: "quesheng",
            6: "huntian", 7: "huntian"}.get(major)
    if not name:
        return None
    key = (int(cat or 0), int(major), int(size))
    if key in _LEVEL_ICONS:
        return _LEVEL_ICONS[key]
    prefix = {1: "sima_", 2: "sanma_"}.get(int(cat or 0), "")
    out = None
    for fname in (f"{prefix}{name}", name):
        fp = _LEVEL_DIR / f"{fname}.png"
        if not fp.is_file():
            continue
        try:
            src = Image.open(fp).convert("RGBA")
            scale = size / max(src.width, src.height)
            w, h = max(1, int(src.width * scale)), max(1, int(src.height * scale))
            im = src.resize((w, h), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(im, ((size - w) // 2, (size - h) // 2), im)
            out = canvas
            break
        except Exception:  # noqa: BLE001
            continue
    _LEVEL_ICONS[key] = out
    return out


def _badge(img, d, x, y, size, th, level_id):
    """段位徽章：有官方图标就贴，没有就自绘六边形。"""
    cat, major, star = parse_level_id(level_id)
    if not major:
        return
    icon = _level_icon(cat, major, size)
    if icon is not None:
        img.paste(icon, (int(x), int(y)), icon)
        if star:
            _stars_row(d, x + size / 2, y + size - 2, star, max(3.5, size * 0.10), th["gold"])
        return
    color = MAJOR_COLORS.get(major, th["gold"])
    cx, cy, r = x + size / 2, y + size / 2, size / 2
    hex_pts = [(cx + r * math.cos(math.radians(60 * i - 90)),
                cy + r * math.sin(math.radians(60 * i - 90))) for i in range(6)]
    d.polygon(hex_pts, fill=color)
    inner = [(cx + r * 0.80 * math.cos(math.radians(60 * i - 90)),
              cy + r * 0.80 * math.sin(math.radians(60 * i - 90))) for i in range(6)]
    d.polygon(inner, fill=th["panel"])
    _txt(d, (cx, cy - size * 0.04), MAJOR_SHORT.get(major, ""), max(9, int(size * 0.36)),
         th, color=color, bold=True, anchor="mm")
    if star:
        gap = max(5, size * 0.13)
        sx = cx - gap * (star - 1) / 2
        for i in range(min(star, 3)):
            dot = max(1.6, size * 0.045)
            px, py = sx + gap * i, cy + size * 0.31
            d.ellipse([px - dot, py - dot, px + dot, py + dot], fill=color)


def _stars_row(d, cx, cy, n, r, color):
    n = max(0, min(int(n or 0), 3))
    gap = r * 2.5
    x0 = cx - gap * (n - 1) / 2
    for i in range(n):
        _star(d, x0 + gap * i, cy, r, color)


def _rank_color(rank: int, th):
    return {1: th["gold"], 2: th["silver"], 3: th["orange"], 4: th["red"]}.get(rank, th["text"])


def _circle_avatar(raw, size, th, fallback="雀"):
    """把头像字节裁成圆形；拿不到就画占位圆。"""
    if not raw:
        return None
    try:
        src = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(src, (0, 0), mask)
        ImageDraw.Draw(out).ellipse((0, 0, size - 1, size - 1), outline=th["gold_dim"], width=2)
        return out
    except Exception:  # noqa: BLE001
        return None


def _header(d, w, title, th, right=""):
    d.rectangle([0, 0, w, 46], fill=_blend(th["panel2"], th["bg"], 0.6))
    d.rectangle([0, 45, w, 47], fill=th["gold_dim"])
    d.rectangle([PAD, 15, PAD + 4, 31], fill=th["gold"])
    _txt(d, (PAD + 14, 23), title, 19, th, color=th["gold"], bold=True, anchor="lm")
    if right:
        _txt(d, (w - PAD, 24), right, 13, th, color=th["sub"], anchor="rm")


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _finish(img, y_end, th):
    """按内容底部裁切；越界用背景色补齐（绝不出现黑边）。"""
    y_end = max(120, int(y_end))
    if y_end > img.height:
        canvas = Image.new("RGB", (img.width, y_end), th["bg"])
        canvas.paste(img, (0, 0))
        img = canvas
    return img.crop((0, 0, img.width, y_end))


def _panel_head(d, x, y, w, title, th, right="", right_color=None, title_color=None):
    """面板统一标题行：左标题 + 右附注（Long 卡片靠它省一行高度）。"""
    _txt(d, (x + 14, y + 18), title, 13, th, color=title_color or th["sub"], anchor="lm")
    if right:
        _txt(d, (x + w - 14, y + 18), right, 12, th,
             color=right_color or th["sub"], anchor="rm", bold=bool(right_color))


# ---------------------------------------------------------------- 各区块

def _player_block(img, d, x, y, w, p, th) -> int:
    """头像 + 昵称 + 段位 + 当前分/升级分 + 徽章 + 进度条；返回占用高度。"""
    size = 54
    av = _circle_avatar(p.get("avatar"), size, th)
    if av is not None:
        img.paste(av, (int(x), int(y)), av)
    else:
        d.ellipse([x, y, x + size, y + size], fill=th["panel2"], outline=th["border"])
        _txt(d, (x + size / 2, y + size / 2 - 1), "雀", 20, th, color=th["sub"], anchor="mm")

    badge = 54
    bx = x + w - badge
    _badge(img, d, bx, y - 2, badge, th, p.get("level_id"))

    # 徽章与右侧数字之间留出空间；昵称可用宽度随之收窄
    name_w = bx - 12 - (x + size + 14)
    name = _ellipsize(d, strip_emoji(p.get("nickname")) or "未知玩家", 21, name_w)
    _txt(d, (x + size + 14, y + 6), name, 21, th, bold=True, anchor="lm")

    lv = str(p.get("level_name") or "")
    cur = int(p.get("level_score") or 0)
    nxt = int(p.get("level_next") or 0)
    sub_bits = [lv] if lv else []
    sub_bits.append(f"{cur} / {nxt}" if nxt else f"{cur} 分")
    if nxt:
        sub_bits.append(f"距升段 {max(0, nxt - cur)}")
    _txt(d, (x + size + 14, y + 34), " · ".join(sub_bits), 14, th,
         color=th["gold"], anchor="lm")

    y += size + 14
    if nxt:
        _progress(d, x, y, w, 10, cur / max(1, nxt), th)
        _txt(d, (x, y + 26), "0", 12, th, color=th["sub"], anchor="lm")
        _txt(d, (x + w / 2, y + 26), f"当前 {cur}", 13, th, color=th["gold"], bold=True, anchor="mm")
        _txt(d, (x + w, y + 26), f"升级 {nxt}", 12, th, color=th["sub"], anchor="rm")
        y += 44
    else:
        _txt(d, (x, y + 12), f"当前 {cur} 分", 13, th, color=th["gold"], bold=True, anchor="lm")
        y += 30
    return int(y)


def _session_bands(d, left, right, top, bottom, n, sessions, th, step) -> None:
    """在曲线图上铺「连打时段」底色（参考图里那几块橙色区域）。

    只标局数 ≥3 的连续段——单局也算一段的话整张图都是色块，反而看不出节奏。
    """
    if not sessions or n < 2:
        return
    band = _blend(th["orange"], th["panel"], 0.13)
    for s in sessions:
        if not s.get("long"):
            continue
        a, b = int(s.get("start_index", 0)), int(s.get("end_index", 0))
        if b <= a:
            continue
        d.rectangle([left + step * a, top, left + step * b, bottom], fill=band)


def _score_curve(d, x, y, w, p, th, h: int = 132) -> int:
    """段位分走势：折线 + 面积 + 跨段分界 + 连打时段底色。"""
    scores = [int(s) for s in (p.get("level_scores") or [])]
    if len(scores) < 2:
        return y
    changes = p.get("rank_changes") or []
    _panel(d, x, y, w, h, th)
    net = p.get("pt_total")
    if net is None:
        delta = scores[-1] - scores[0]
        head, head_color = f"{scores[0]} → {scores[-1]}（{delta:+d}）", (
            th["gold"] if delta >= 0 else th["red"])
    else:                                   # 有跨段时「首末之差」没有意义，改显示净 PT
        head, head_color = f"净 PT {int(net):+d} · 当前 {scores[-1]}", (
            th["gold"] if int(net) >= 0 else th["red"])
    _panel_head(d, x, y, w, f"段位分走势（最近 {len(scores)} 局）", th, head, head_color)

    left, right = x + 58, x + w - 16
    top, bottom = y + 36, y + h - 16
    lo, hi = min(scores), max(scores)
    if hi - lo < 30:                      # 波动太小则放大 y 轴，避免把 5 分抖动画成断崖
        mid = (hi + lo) / 2
        lo, hi = mid - 15, mid + 15
    span = max(1, hi - lo)
    step = (right - left) / max(1, len(scores) - 1)
    pts = [(left + step * i, bottom - (v - lo) / span * (bottom - top))
           for i, v in enumerate(scores)]

    _session_bands(d, left, right, top, bottom, len(scores), p.get("sessions"), th, step)
    d.line([left, top, right, top], fill=th["grid"], width=1)
    d.line([left, bottom, right, bottom], fill=th["grid"], width=1)
    _txt(d, (left - 10, top), str(hi), 11, th, color=th["sub"], anchor="rm")
    _txt(d, (left - 10, bottom - 7), str(lo), 11, th, color=th["sub"], anchor="rm")

    d.polygon(pts + [(right, bottom), (left, bottom)], fill=_blend(th["gold"], th["panel"], 0.16))
    # ★ 跨段处**断开**折线：段位分本身会在升/降段时重置，硬连起来会像「一夜暴跌」。
    breaks = sorted({int(c.get("index", -1)) for c in changes} & set(range(1, len(pts))))
    seg_start = 0
    for b in breaks + [len(pts)]:
        seg = pts[seg_start:b]
        if len(seg) > 1:
            d.line(seg, fill=th["gold"], width=2)
        seg_start = b

    for ch in changes:
        idx = int(ch.get("index", -1))
        if not (0 <= idx < len(scores)):
            continue
        cx = left + step * idx
        for yy in range(int(top), int(bottom), 8):
            d.line([cx, yy, cx, min(yy + 4, bottom)], fill=th["gold_dim"], width=1)
        label = str(ch.get("name") or "")
        if label and len(changes) <= 6:
            _txt(d, (min(cx + 3, right - 40), top + 8), label, 10, th,
                 color=th["gold"], anchor="lt")

    r_dot = 2.4 if len(pts) <= 40 else 1.9
    for i, (px, py) in enumerate(pts):
        last = i == len(pts) - 1
        r = r_dot + 1.4 if last else r_dot
        d.ellipse([px - r, py - r, px + r, py + r],
                  fill=th["gold"] if last else th["gold_dim"])
    return y + h + COL_GAP


def _point_curve(d, x, y, w, p, th, h: int = 118) -> int:
    """素点走势：每局终局点数折线，25000 基准线（读「打得稳不稳」比段位分更直接）。"""
    rows = p.get("game_rows") or []
    vals = [int(r["score"]) for r in rows if r.get("score") is not None]
    if len(vals) < 2:
        return y
    _panel(d, x, y, w, h, th)
    avg = round(sum(vals) / len(vals))
    _panel_head(d, x, y, w, "素点走势（终局点数）", th, f"平均 {avg:,}",
                th["green"] if avg >= 25000 else th["red"])

    left, right = x + 58, x + w - 16
    top, bottom = y + 34, y + h - 16
    lo = min(min(vals), 25000)
    hi = max(max(vals), 25000)
    if hi - lo < 4000:
        mid = (hi + lo) / 2
        lo, hi = mid - 2000, mid + 2000
    span = max(1, hi - lo)
    step = (right - left) / max(1, len(vals) - 1)
    pts = [(left + step * i, bottom - (v - lo) / span * (bottom - top))
           for i, v in enumerate(vals)]

    y_base = bottom - (25000 - lo) / span * (bottom - top)
    _session_bands(d, left, right, top, bottom, len(vals), p.get("sessions"), th, step)
    d.line([left, top, right, top], fill=th["grid"], width=1)
    d.line([left, bottom, right, bottom], fill=th["grid"], width=1)
    for xx in range(int(left), int(right), 10):          # 基准线画虚线，别和走势混淆
        d.line([xx, y_base, min(xx + 5, right), y_base], fill=th["silver"], width=1)
    _txt(d, (left - 10, y_base), "25k", 10, th, color=th["silver"], anchor="rm")
    _txt(d, (left - 10, top), f"{hi // 1000}k", 10, th, color=th["sub"], anchor="rm")
    _txt(d, (left - 10, bottom - 2), f"{lo // 1000}k", 10, th, color=th["sub"], anchor="rm")

    if len(pts) > 1:
        d.line(pts, fill=th["blue"], width=2)
    r_dot = 2.2 if len(pts) <= 40 else 1.7
    for i, (px, py) in enumerate(pts):
        last = i == len(pts) - 1
        r = r_dot + 1.2 if last else r_dot
        d.ellipse([px - r, py - r, px + r, py + r],
                  fill=th["blue"] if last else _blend(th["blue"], th["panel"], 0.55))
    return y + h + COL_GAP


def _pt_bars(d, x, y, w, p, th, h: int = 108) -> int:
    """逐局 PT 柱状：零轴上下分色，一眼看出「哪几局在流血」。"""
    rows = p.get("game_rows") or []
    vals = [int(r["pt"]) for r in rows]
    if len(vals) < 2:
        return y
    _panel(d, x, y, w, h, th)
    win = sum(1 for v in vals if v > 0)
    _panel_head(d, x, y, w, "逐局 PT", th, f"正 {win} / 负 {len(vals) - win} 局")

    left, right = x + 40, x + w - 16
    top, bottom = y + 32, y + h - 14
    lo, hi = min(min(vals), 0), max(max(vals), 0)
    if hi - lo < 40:
        mid = (hi + lo) / 2
        lo, hi = mid - 20, mid + 20
    span = max(1, hi - lo)
    zero = bottom - (0 - lo) / span * (bottom - top)
    step = (right - left) / max(1, len(vals))
    bw = max(1.5, min(6.0, step * 0.62))

    _session_bands(d, left, right, top, bottom, len(vals), p.get("sessions"), th, step)
    d.line([left, zero, right, zero], fill=th["border"], width=1)
    _txt(d, (left - 8, top + 2), f"{int(round(hi)):+d}", 10, th, color=th["sub"], anchor="rm")
    _txt(d, (left - 8, bottom - 10), f"{int(round(lo)):+d}", 10, th, color=th["sub"], anchor="rm")
    for i, v in enumerate(vals):
        cx = left + step * (i + 0.5)
        vy = bottom - (v - lo) / span * (bottom - top)
        d.rectangle([cx - bw / 2, min(vy, zero), cx + bw / 2, max(vy, zero)],
                    fill=th["green"] if v < 0 else (th["gold"] if v > 0 else th["sub"]))
    return y + h + COL_GAP


def _rank_plot(d, x, y, w, p, th, h: int = 132) -> int:
    """顺位走势：横轴局数、纵轴顺位，折线串点。"""
    seq = [int(s) for s in (p.get("seq") or [])]
    if not seq:
        return y
    n_rank = 4 if p.get("four") else 3
    _panel(d, x, y, w, h, th)
    _panel_head(d, x, y, w, f"最近 {len(seq)} 局顺位走势", th,
                "上位" if sum(seq) / len(seq) <= (n_rank + 1) / 2 else "下位",
                th["gold"] if sum(seq) / len(seq) <= (n_rank + 1) / 2 else th["red"])
    left, right, top = x + 42, x + w - 18, y + 34
    plot_h = h - 58
    for r in range(1, n_rank + 1):
        yy = top + (r - 1) * (plot_h / max(1, n_rank - 1))
        d.line([left, yy, right, yy], fill=th["grid"], width=1)
        _txt(d, (left - 12, yy), str(r), 12, th, color=th["sub"], anchor="rm")

    n = len(seq)
    step = (right - left) / max(1, n - 1)
    pts = []
    for i, r in enumerate(seq):
        r = min(max(r, 1), n_rank)
        pts.append((left + step * i, top + (r - 1) * (plot_h / max(1, n_rank - 1)), r))
    if len(pts) > 1:
        d.line([(px, py) for px, py, _ in pts], fill=th["border"], width=2)
    pr = 4 if n <= 24 else (3 if n <= 40 else 2)
    for px, py, r in pts:
        d.ellipse([px - pr, py - pr, px + pr, py + pr], fill=_rank_color(r, th))
    return y + h + COL_GAP


def _kpi_grid(d, x, y, w, items, th, cols=2) -> int:
    """KPI 小卡网格：标题在上、大数字在中、副标题压底部。"""
    if not items:
        return y
    rows = math.ceil(len(items) / cols)
    gap = 10
    bw = (w - (cols - 1) * gap) // cols
    inner = bw - 24
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        cx = x + c * (bw + gap)
        cy = y + r * (78 + gap)
        _panel(d, cx, cy, bw, 78, th, fill=th["panel2"])
        _txt(d, (cx + 12, cy + 17), _ellipsize(d, it.get("label") or "", 12, inner), 12, th,
             color=th["sub"], anchor="lm")
        color = it.get("color") or th["text"]
        _txt(d, (cx + 12, cy + 42), _ellipsize(d, it.get("value") or "—", 21, inner), 21, th,
             color=color, bold=True, anchor="lm")
        if it.get("sub"):
            _txt(d, (cx + bw - 12, cy + 64), _ellipsize(d, it["sub"], 11, inner), 11, th,
                 color=th["sub"], anchor="rm")
    return y + rows * (78 + gap) - gap + COL_GAP


def _overview_panel(d, x, y, w, p, th) -> int:
    """全局总结：均顺 / 一位率 / 顺位波动 / 平均素点。"""
    s = p.get("summary") or {}
    if not s.get("games"):
        return y
    stab = s.get("stability") or {}
    items = [
        {"label": "平均顺位", "value": f"{float(s.get('avg_rank') or 0):.2f}",
         "color": th["gold"] if float(s.get("avg_rank") or 9) <= 2.5 else th["text"],
         "sub": f"共 {int(s.get('games') or 0)} 局"},
        {"label": "一位率", "value": f"{float(s.get('win_rate') or 0) * 100:.0f}%",
         "color": th["green"],
         "sub": f"四位 {float(s.get('last_rate') or 0) * 100:.0f}%"},
        {"label": "顺位波动", "value": f"{float(stab.get('sd') or 0):.2f}",
         "sub": stab.get("label") or ""},
        {"label": "平均素点", "value": f"{int(s.get('avg_score') or 0) // 1000}k",
         "color": th["green"] if int(s.get("avg_score") or 0) >= 25000 else th["red"],
         "sub": f"最高 {int(s.get('max_score') or 0) // 1000}k"},
    ]
    return _kpi_grid(d, x, y, w, items, th, cols=2)


def _rank_cards_vert(d, x, y, w, p, th) -> int:
    """顺位分布（竖排）：一位～四位的次数、占比与进度条。"""
    seq = [int(s) for s in (p.get("seq") or [])]
    if not seq:
        return y
    four = bool(p.get("four"))
    labels = [(1, "一位"), (2, "二位"), (3, "三位"), (4, "四位")] if four else \
             [(1, "一位"), (2, "二位"), (3, "三位")]
    row_h = 27
    h = 34 + len(labels) * row_h
    _panel(d, x, y, w, h, th)
    _panel_head(d, x, y, w, "顺位分布", th, f"{len(seq)} 局")
    bar_x = x + 66
    bar_w = w - 66 - 78
    for i, (k, label) in enumerate(labels):
        cnt = seq.count(k)
        pct = cnt / max(1, len(seq))
        cy = y + 40 + i * row_h
        color = _rank_color(k, th)
        _txt(d, (x + 14, cy), label, 13, th, color=th["text"], anchor="lm")
        _progress(d, bar_x, cy - 5, bar_w, 10, pct, th, color=color)
        _txt(d, (x + w - 14, cy), f"{cnt} 局 {pct * 100:.0f}%", 12, th,
             color=color, anchor="rm", bold=True)
    return y + h + COL_GAP


def _session_panel(d, x, y, w, p, th) -> int:
    """连打 vs 散打：长时间连打是否明显掉分（参考图里最有价值的一栏）。"""
    s = p.get("summary") or {}
    long_s = s.get("long") or {}
    casual_s = s.get("casual") or {}
    lg, cs = int(long_s.get("games") or 0), int(casual_s.get("games") or 0)
    if not (lg or cs):
        return y
    rows = []
    if lg:
        rows.append(("连打", lg, long_s.get("avg_rank"), int(long_s.get("pt") or 0), th["orange"]))
    if cs:
        rows.append(("散打", cs, casual_s.get("avg_rank"), int(casual_s.get("pt") or 0), th["silver"]))
    diff = None
    if lg and cs and long_s.get("avg_rank") is not None and casual_s.get("avg_rank") is not None:
        diff = float(long_s["avg_rank"]) - float(casual_s["avg_rank"])
    h = 34 + len(rows) * 30 + (22 if diff is not None and abs(diff) >= 0.15 else 0)
    _panel(d, x, y, w, h, th)
    _panel_head(d, x, y, w, "连打 vs 散打", th, f"共 {int(s.get('session_count') or 0)} 段")
    for i, (label, games, ar, pt, color) in enumerate(rows):
        cy = y + 42 + i * 30
        # 药丸要留够字高：11px 中文塞进 12px 高的圆角矩形会被切掉上下缘
        d.rounded_rectangle([x + 14, cy - 9, x + 60, cy + 9], radius=9, fill=color)
        _txt(d, (x + 37, cy), label, 12, th, color=th["bg"], anchor="mm")
        detail = f"{games} 局"
        if ar is not None:
            detail += f" · 均顺 {float(ar):.2f}"
        _txt(d, (x + 68, cy), detail, 12, th, color=th["text"], anchor="lm")
        _txt(d, (x + w - 14, cy), f"{pt:+d}", 13, th,
             color=th["green"] if pt >= 0 else th["red"], anchor="rm", bold=True)
    if diff is not None and abs(diff) >= 0.15:
        hint = ("连打明显吃亏，打几局就歇一下" if diff > 0 else "连打手感更好，可以顺势多打")
        _txt(d, (x + 14, y + h - 13), _ellipsize(d, hint, 11, w - 28), 11, th,
             color=th["orange"] if diff > 0 else th["green"], anchor="lm")
    return y + h + COL_GAP


def _top_session_panel(d, x, y, w, p, th) -> int:
    """最长连打段：局数、时长、均顺、净 PT。"""
    s = p.get("summary") or {}
    top = s.get("top_session")
    if not top or int(top.get("games") or 0) < 3:
        return y
    h = 64
    _panel(d, x, y, w, h, th, fill=th["panel2"])
    _txt(d, (x + 14, y + 18), "最长连打段", 12, th, color=th["sub"], anchor="lm")
    try:
        started = top["started"].astimezone().strftime("%m-%d %H:%M")
    except Exception:                       # noqa: BLE001
        started = str(top.get("started") or "")
    _txt(d, (x + w - 14, y + 18), started, 12, th, color=th["sub"], anchor="rm")
    ar = top.get("avg_rank")
    pt = int(top.get("pt") or 0)
    bits = [f"{int(top.get('games') or 0)} 局",
            f"{float(top.get('hours') or 0):.1f} 小时"]
    if ar is not None:
        bits.append(f"均顺 {float(ar):.2f}")
    _txt(d, (x + 14, y + 42), " · ".join(bits), 13, th, color=th["text"], anchor="lm")
    _txt(d, (x + w - 14, y + 42), f"{pt:+d} PT", 14, th,
         color=th["green"] if pt >= 0 else th["red"], anchor="rm", bold=True)
    return y + h + COL_GAP


def _hour_panel(d, x, y, w, p, th) -> int:
    """打牌时段：四个时段的局数与均顺。局数太少时时段分布没有统计意义，直接省略。"""
    hours = p.get("hours") or []
    total = sum(int(h.get("games") or 0) for h in hours)
    if total < 8 or len([h for h in hours if int(h.get("games") or 0)]) < 2:
        return y
    row_h = 26
    h = 34 + len(hours) * row_h
    _panel(d, x, y, w, h, th)
    _panel_head(d, x, y, w, "打牌时段", th, "局数 · 均顺")
    bar_x = x + 62
    bar_w = w - 62 - 92
    for i, hr in enumerate(hours):
        cy = y + 40 + i * row_h
        games = int(hr.get("games") or 0)
        _txt(d, (x + 14, cy), str(hr.get("name") or ""), 12, th, color=th["text"], anchor="lm")
        _progress(d, bar_x, cy - 5, bar_w, 10, games / max(1, total), th,
                  color=th["blue"] if games else th["grid"])
        ar = hr.get("avg_rank")
        tail = f"{games} 局" + (f" · {float(ar):.2f}" if games and ar is not None else "")
        _txt(d, (x + w - 14, cy), tail, 11, th, color=th["sub"], anchor="rm")
    return y + h + COL_GAP


def _summary_row(d, x, y, w, p, th) -> int:
    s = p.get("summary") or {}
    bits = [f"共 {int(s.get('games') or 0)} 局"]
    if s.get("avg_rank"):
        bits.append(f"平均顺位 {float(s['avg_rank']):.2f}")
    if s.get("win_rate") is not None:
        bits.append(f"一位率 {float(s['win_rate']) * 100:.0f}%")
    if s.get("top2_rate") is not None:
        bits.append(f"连对率 {float(s['top2_rate']) * 100:.0f}%")
    if s.get("best_streak"):
        bits.append(f"最长连一位 {int(s['best_streak'])}")
    if s.get("level_delta"):
        bits.append(f"段位分 {int(s['level_delta']):+d}")
    if not bits:
        return y
    _panel(d, x, y, w, 34, th, fill=th["panel2"])
    _txt(d, (x + 14, y + 17), _ellipsize(d, " · ".join(bits), 13, w - 28), 13, th, anchor="lm")
    return y + 44


def _metrics_panel(d, x, y, w, p, th) -> int:
    """关键指标对比：每行「名称 / 本人值 / 段位均值」，本人值绿优红劣。"""
    rows = p.get("metrics") or []
    if not rows:
        return y
    cols = 3 if w >= 700 else 2
    per_col = math.ceil(len(rows) / cols)
    col_w = (w - 20) // cols
    row_h = 30
    h = 38 + per_col * row_h
    _panel(d, x, y, w, h, th)
    _txt(d, (x + 14, y + 18), "关键指标对比", 14, th, color=th["gold"], bold=True, anchor="lm")
    _txt(d, (x + w - 14, y + 18), str(p.get("metrics_scope") or "同段位均值"),
         12, th, color=th["sub"], anchor="rm")
    d.line([x + 10, y + 30, x + w - 10, y + 30], fill=th["grid"], width=1)

    for i, m in enumerate(rows):
        col, row = divmod(i, per_col)
        x0 = x + 10 + col * col_w
        cy = y + 44 + row * row_h
        _txt(d, (x0, cy), _ellipsize(d, m.get("label", ""), 12, col_w * 0.44), 12, th,
             color=th["sub"], anchor="lm")
        val = str(m.get("value", "-"))
        better = m.get("better")
        color = th["green"] if better is True else (th["red"] if better is False else th["text"])
        _txt(d, (x0 + col_w * 0.60, cy), val, 13, th, color=color, bold=True, anchor="rm")
        avg = str(m.get("avg", ""))
        if avg and avg != "-":
            _txt(d, (x0 + col_w - 14, cy), avg, 11, th, color=th["sub"], anchor="rm")
    return y + h + COL_GAP


_RADAR_AXES = ("进攻", "防守", "副露", "打点", "速度")


def _radar(d, x, y, w, p, th, h: int = 246) -> int:
    """牌风雷达：本人多边形 + 段位均值（50 基准）虚线多边形。

    `h` 可由调用方给（和「详细档案」并排时拉高，把雷达图和右侧条一起撑开，
    避免雷达下面空出一大块）。
    """
    vals = p.get("radar") or []
    if len(vals) != len(_RADAR_AXES):
        return y
    h = max(200, int(h))
    _panel(d, x, y, w, h, th)
    title = "牌风雷达" + (f"（{p.get('radar_base') or '同段位'}=50）" if p.get("radar_base") else "")
    _txt(d, (x + 14, y + 18), title, 13, th, color=th["sub"], anchor="lm")

    body_top, body_bottom = y + 40, y + h - 16
    body_h = body_bottom - body_top
    # 半径必须给轴标签留位置：标签在 r+20 处、还得算上字高，否则「进攻」会压到标题、
    # 「防守」会贴到右边的条形名上（看起来像重复了两个「防守」）
    r = max(46, min(96, int(body_h / 2 - 34)))
    cx, cy = x + 14 + r + 26, (body_top + body_bottom) / 2
    n = len(_RADAR_AXES)
    angles = [math.radians(-90 + i * 360 / n) for i in range(n)]

    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = [(cx + r * ring * math.cos(a), cy + r * ring * math.sin(a)) for a in angles]
        d.polygon(pts, outline=th["grid"])
    for i, a in enumerate(angles):
        d.line([cx, cy, cx + r * math.cos(a), cy + r * math.sin(a)], fill=th["grid"], width=1)
        lx, ly = cx + (r + 18) * math.cos(a), cy + (r + 18) * math.sin(a)
        anchor = "mm"
        if abs(math.cos(a)) > 0.4:
            anchor = "lm" if math.cos(a) > 0 else "rm"
        _txt(d, (lx, ly), _RADAR_AXES[i], 12, th, color=th["sub"], anchor=anchor)

    def poly(values, color, fill_t):
        pts = []
        for a, v in zip(angles, values):
            rr = r * max(0.03, min(100.0, float(v))) / 100.0
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        d.polygon(pts, fill=_blend(color, th["panel"], fill_t), outline=color)
        return pts

    bpts = poly([50] * n, th["silver"], 0.0)
    for i in range(n):                      # 段位均值画成虚线（手工短段）
        x1, y1 = bpts[i]
        x2, y2 = bpts[(i + 1) % n]
        seg = 6
        for k in range(seg):
            if k % 2:
                continue
            d.line([x1 + (x2 - x1) * k / seg, y1 + (y2 - y1) * k / seg,
                    x1 + (x2 - x1) * (k + 1) / seg, y1 + (y2 - y1) * (k + 1) / seg],
                   fill=th["silver"], width=1)
    pts = poly(vals, th["blue"], 0.22)
    for px, py in pts:
        d.ellipse([px - 2.5, py - 2.5, px + 2.5, py + 2.5], fill=th["blue"])

    lx = cx + r + 62                # 留出雷达右轴标签的宽度，否则「防守」会贴住条形名
    # 雷达本身只用左半边；通栏时让条形延伸到面板右缘（否则右边空一大片）
    bar_right = x + w - 14
    if bar_right - lx > 640:                 # 太宽就把整组居中，别拉成横贯全图的长棍
        shift = ((bar_right - lx) - 640) // 2
        lx += shift
        bar_right -= shift
    bar_span = max(1, body_bottom - body_top - 20)
    step_y = bar_span / max(1, len(_RADAR_AXES))
    for i, (name, v) in enumerate(zip(_RADAR_AXES, vals)):
        yy = body_top + 14 + i * step_y
        _txt(d, (lx, yy), name, 12, th, color=th["sub"], anchor="lm")
        score = float(v)
        color = th["green"] if score >= 55 else (th["red"] if score <= 45 else th["text"])
        _txt(d, (bar_right, yy), f"{score:.0f}", 15, th, color=color, bold=True, anchor="rm")
        _progress(d, lx, yy + 15, bar_right - lx, 5, score / 100.0, th, color=color)
    return y + h + COL_GAP


def _detail_panel(d, x, y, w, p, th) -> int:
    """详细档案：单列键值对，把参考图底部那堆细项摊平。"""
    s = p.get("summary") or {}
    if not s.get("games"):
        return y
    wr = float(s.get("win_rate") or 0)
    t2 = float(s.get("top2_rate") or 0)
    lr = float(s.get("last_rate") or 0)
    rows = [
        ("总对局", f"{int(s.get('games') or 0)} 局"),
        ("最近对战", f"{int(s.get('idle_days') or 0)} 天前" if s.get("idle_days") is not None else "—"),
        ("统计跨度", f"{float(s.get('span_days') or 0):.1f} 天"),
        ("日均局数", f"{float(s.get('per_day') or 0):.1f} 局"),
        ("平均间隔", f"{float(s.get('gap_median') or 0):.0f} 分" if s.get("gap_median") else "—"),
        ("连打段数", f"{int(s.get('session_count') or 0)} 段"),
        ("一/二/三/四位", f"{wr * 100:.0f}/{max(0.0, (t2 - wr)) * 100:.0f}"
                          f"/{max(0.0, 1 - t2 - lr) * 100:.0f}/{lr * 100:.0f}%"),
        ("平均顺位", f"{float(s.get('avg_rank') or 0):.2f}"),
        ("最长连一位", f"{int(s.get('best_streak') or 0)} 局"),
        ("最长连最下位", f"{int(s.get('worst_streak') or 0)} 局"),
        ("素点区间", f"{int(s.get('min_score') or 0) // 1000}k ~ {int(s.get('max_score') or 0) // 1000}k"),
        ("素点均值", f"{int(s.get('avg_score') or 0):,}"),
    ]
    # 「跨度/日均/间隔」在局数很少时基本是噪音（2 局算出 0.0 天日均没意义），省掉
    if int(s.get("games") or 0) < 5:
        drop = {"统计跨度", "日均局数", "平均间隔"}
        rows = [r for r in rows if r[0] not in drop]
    row_h = 26
    h = 34 + len(rows) * row_h
    _panel(d, x, y, w, h, th)
    _txt(d, (x + 14, y + 18), "详细档案", 13, th, color=th["gold"], bold=True, anchor="lm")
    d.line([x + 10, y + 30, x + w - 10, y + 30], fill=th["grid"], width=1)
    for i, (label, value) in enumerate(rows):
        cy = y + 44 + i * row_h
        _txt(d, (x + 14, cy), label, 12, th, color=th["sub"], anchor="lm")
        _txt(d, (x + w - 14, cy), value, 12, th, color=th["text"], anchor="rm")
    return y + h + COL_GAP


def _footer(d, x, y, w, p, th) -> int:
    note = str(p.get("footer") or "数据来自雀魂牌谱屋（amae-koromo）· PT=本局段位分变动")
    _txt(d, (x, y + 8), _ellipsize(d, note, 11, w), 11, th, color=th["sub"], anchor="lm")
    return y + 30


# ---------------------------------------------------------------- AI 锐评卡

_AI_FONT = 15
_AI_LINE_H = 27
_AI_BLANK_H = 12


def _chip_row(d, y, chips, th, w=W_NARROW) -> int:
    """一排小标签（关键指标摘要），放不下就截断，绝不换行溢出。"""
    x, h = PAD, 26
    for c in chips:
        cw = _tw(d, c, 12) + 20
        if x + cw > w - PAD:
            break
        d.rounded_rectangle([x, y, x + cw, y + h], radius=h // 2,
                            fill=th["panel2"], outline=th["border"], width=1)
        _txt(d, (x + cw / 2, y + h / 2 - 1), c, 12, th, color=th["sub"], anchor="mm")
        x += cw + 8
    return y + h + 10


def _paint_comment(img, d, p, th) -> int:
    """绘制 AI 锐评卡主体，返回内容底部 y（唯一绘制路径，供两遍法复用）。"""
    w = W_NARROW
    _header(d, w, "AI 锐评", th, str(p.get("scope") or ""))
    y = 60

    size = 44
    av = _circle_avatar(p.get("avatar"), size, th)
    if av is not None:
        img.paste(av, (PAD, int(y)), av)
    else:
        d.ellipse([PAD, y, PAD + size, y + size], fill=th["panel2"], outline=th["border"])
        _txt(d, (PAD + size / 2, y + size / 2 - 1), "雀", 17, th, color=th["sub"], anchor="mm")
    name = _ellipsize(d, strip_emoji(p.get("nickname")) or "未知玩家", 18, 400)
    _txt(d, (PAD + size + 12, y + 6), name, 18, th, bold=True, anchor="lm")
    lv = str(p.get("level_name") or "")
    if lv:
        _txt(d, (PAD + size + 12, y + 30), lv, 13, th, color=th["gold"], anchor="lm")
    _badge(img, d, w - PAD - 40, y + 2, 40, th, p.get("level_id"))
    y += size + 14

    chips = [c for c in (p.get("chips") or []) if c]
    if chips:
        y = _chip_row(d, y, chips, th, w)

    text = strip_emoji(strip_markdown(p.get("text")))
    max_chars = int(p.get("max_chars") or 600)          # 兜底截断：模型偶尔啰嗦
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    inner_x, inner_w = PAD + 16, w - 2 * PAD - 32
    lines = _wrap(d, text, _AI_FONT, inner_w) if text else []
    if not lines:                                     # 空白/纯符号也算「没内容」
        lines = ["（AI 没有返回内容）"]
    heights = [_AI_BLANK_H if ln == "" else _AI_LINE_H for ln in lines]
    body_h = 32 + sum(heights)
    _panel(d, PAD, y, w - 2 * PAD, body_h, th, fill=th["panel2"])
    d.rounded_rectangle([PAD + 1, y + 12, PAD + 5, y + body_h - 12], radius=2, fill=th["gold"])
    ty = y + 16
    for ln, hh in zip(lines, heights):
        if ln:
            _txt(d, (inner_x, ty), ln, _AI_FONT, th, anchor="lt")
        ty += hh
    y += body_h + 12

    note = " · ".join(x for x in (
        f"模型 {p['model']}" if p.get("model") else "",
        "AI 生成，仅供参考",
    ) if x)
    _txt(d, (PAD, y + 6), _ellipsize(d, note, 11, w - 2 * PAD), 11, th,
         color=th["sub"], anchor="lm")
    return y + 26


def render_comment(p: dict) -> bytes:
    """渲染 AI 锐评卡，返回 PNG 字节。

    payload：`nickname / level_name / level_id / avatar / scope / chips / text / model / theme`。
    高度采用「先估后绘 + 按实测重绘」两遍法，内容不会被裁，底部也不留大片空白。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = THEMES.get(str(p.get("theme") or "dark").lower(), THEMES["dark"])
    probe = Image.new("RGB", (W_NARROW, 8), th["bg"])
    pd = ImageDraw.Draw(probe)
    raw = strip_emoji(strip_markdown(p.get("text")))
    if len(raw) > int(p.get("max_chars") or 600):
        raw = raw[: int(p.get("max_chars") or 600)].rstrip() + "…"
    n_lines = max(1, len(_wrap(pd, raw, _AI_FONT, W_NARROW - 2 * PAD - 32))) if raw else 1
    est = 60 + 58 + (36 if p.get("chips") else 0) + (32 + n_lines * _AI_LINE_H) + 50
    img = Image.new("RGB", (W_NARROW, min(H_MAX, max(240, est))), th["bg"])
    d = ImageDraw.Draw(img)
    y = _paint_comment(img, d, p, th)
    if y + 10 > img.height:
        img = Image.new("RGB", (W_NARROW, min(H_MAX, y + 12)), th["bg"])
        d = ImageDraw.Draw(img)
        y = _paint_comment(img, d, p, th)
    return _png(_finish(img, y + 6, th))


# ---------------------------------------------------------------- 入口

def _right_column_h(d, x, y, w, p, th, full: bool) -> int:
    """右栏（结论区）整列绘制，返回底部 y。也用于在测量画布上预估高度。"""
    y = _overview_panel(d, x, y, w, p, th)
    y = _rank_cards_vert(d, x, y, w, p, th)
    y = _session_panel(d, x, y, w, p, th)
    y = _top_session_panel(d, x, y, w, p, th)
    if full:
        y = _hour_panel(d, x, y, w, p, th)
    return y


def render_record(p: dict) -> bytes:
    """渲染战绩走势卡，返回 PNG 字节。

    payload 关键字段：
    `nickname / level_name / level_id / level_score / level_next / four / scope /
    seq(顺位序列) / level_scores(局后段位分) / rank_changes / summary /
    game_rows(逐局流水) / sessions(连打分段) / hours(时段) /
    metrics / metrics_scope / radar / radar_base / avatar(字节) / theme / detail / footer`。

    `game_rows` / `sessions` / `hours` 缺失时，对应区块自动省略（老数据兼容）。
    `detail`：`full`（默认）/ `compact`（省掉逐局 PT、打牌时段、详细档案）。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = THEMES.get(str(p.get("theme") or "dark").lower(), THEMES["dark"])
    full = str(p.get("detail") or "full").lower() != "compact"
    lw, rw = _split_w(W)
    lx, rx = PAD, PAD + lw + GAP

    img = Image.new("RGB", (W, H_MAX), th["bg"])
    d = ImageDraw.Draw(img)

    _header(d, W, "雀魂战绩", th, p.get("scope", ""))
    y = _player_block(img, d, PAD, 60, W - 2 * PAD, p, th)
    y += 8

    # ---- 先量右栏（内容高度由数据决定，不能压缩），再让左栏图表对齐它 ----
    probe = ImageDraw.Draw(Image.new("RGB", (W, 8), th["bg"]))
    ry_end = _right_column_h(probe, rx, y, rw, p, th, full)
    right_h = ry_end - y

    # ---- 左栏：走势（高度按局数缩放，再把高差摊给图表，避免左栏留白）----
    n = max(len(p.get("level_scores") or []), len(p.get("seq") or []))
    scale = _chart_scale(n)
    plan = [("score", "_score_curve", _score_curve, len(p.get("level_scores") or []) >= 2),
            ("point", "_point_curve", _point_curve, len(p.get("game_rows") or []) >= 2),
            ("pt", "_pt_bars", _pt_bars, full and len(p.get("game_rows") or []) >= 2),
            ("rank", "_rank_plot", _rank_plot, bool(p.get("seq")))]
    live = [k for k, _, _, on in plan if on]
    base = [max(_CHART_MIN[k], int(_CHART_H[k] * scale)) + COL_GAP for k in live]
    cap = [int(_CHART_CAP[k] * scale) + COL_GAP for k in live]
    heights = _fit_heights(base, cap, right_h) if live else []

    ly = y
    for (k, _, fn, on), hh in zip([x for x in plan if x[0] in live], heights):
        ly = fn(d, lx, ly, lw, p, th, h=hh - COL_GAP)
    if not live:                                # 连折线图都画不了，退回直接渲染一遍右栏
        _right_column_h(d, rx, y, rw, p, th, full)
        ly = y

    # ---- 右栏：真实绘制 ----
    ry = _right_column_h(d, rx, y, rw, p, th, full)

    y = max(ly, ry)
    y = _summary_row(d, PAD, y, W - 2 * PAD, p, th)
    if p.get("metrics"):
        y = _metrics_panel(d, PAD, y, W - 2 * PAD, p, th)

    # ---- 通栏底部：雷达 + 详细档案并排（雷达拉高对齐档案，别在雷达下面留白）----
    has_radar = len(p.get("radar") or []) == len(_RADAR_AXES)
    if has_radar and full:
        bw = int((W - 2 * PAD - GAP) * 0.52)
        dw = W - 2 * PAD - GAP - bw
        detail_end = _detail_panel(probe, PAD + bw + GAP, y, dw, p, th)
        r2 = _radar(d, PAD, y, bw, p, th, h=detail_end - y - COL_GAP)
        d2 = _detail_panel(d, PAD + bw + GAP, y, dw, p, th)
        y = max(r2, d2)
    elif has_radar:
        y = _radar(d, PAD, y, W - 2 * PAD, p, th)
    elif full:
        y = _detail_panel(d, PAD, y, W - 2 * PAD, p, th)

    y = _footer(d, PAD, y, W - 2 * PAD, p, th)
    return _png(_finish(img, y + 6, th))