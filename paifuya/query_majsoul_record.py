# -*- coding: utf-8 -*-
"""`/雀魂战绩` —— 战绩走势图的数据组装（牌谱屋 → payload → 卡片）。

与 `query_majsoul_pt_plot` 的分工：
- **拿数据**沿用同一套 `paifuya_api`（`search_player` / `player_records`），需要牌谱屋 Key；
- **怎么画**全部交给同目录的 `record_card`，本模块只负责把记录整理成：
  逐局局后段位分、顺位序列、跨段分界、段位均值对比指标、牌风雷达。

发送顺序：**先发战绩卡，再发 AI 锐评卡**（后者失败不影响前者）。

局后段位分的推算口径：
牌谱屋的逐局记录只给「该局结算前的段位」与「本局段位分变动（gradingScore）」，不给局前分。
因而是**从末尾倒推**：末局局后分用 `search_player` 的官方当前分锚定，往前逐局减掉本局变动；
遇到段位发生变化的那一局（说明其间发生升/降段），改取新段位的初始分（= 该段位上限的一半）。
这样曲线末端与「当前分数」严格一致，跨段的台阶也落在正确位置。
官方分拿不到时才退化为「从首局段位初始分正向累加」。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AbstractSet, Optional

from astrbot.api import logger
from httpx import HTTPError, HTTPStatusError

from ..config import conf, normalize_api_key
from ..errors import PaifuyaRateLimitError, QueryError
from . import record_analytics, record_card
from .data.api import paifuya_api as api
from .data.models.game_record import GameRecord
from .data.models.player_num import PlayerNum
from .data.models.player_rank import PlayerRank
from .data.models.room_rank import all_four_player_room_rank, all_three_player_room_rank, RoomRank
from .mappers.player_stats import fix_level

RECORD_PAGE_SIZE = 500
_PEER_TTL = 6 * 3600                  # 段位均值本地缓存 6 小时
_peer_cache: dict[int, tuple[float, dict]] = {}

RECORD_KEY_REQUIRED = (
    '战绩走势需要牌谱屋 API Key（要逐局读取牌谱记录）。'
    '请管理员填写 majsoul_paifuya_api_key 并重载插件。'
)

_MAJOR_NAMES = {1: '初心', 2: '雀士', 3: '雀杰', 4: '雀豪', 5: '雀圣', 6: '魂天', 7: '魂天'}


def level_name(level_id: int) -> str:
    """段位 id → `雀豪2星` / `魂天Lv3`。"""
    try:
        lid = int(level_id or 0)
    except (TypeError, ValueError):
        return '未知段位'
    major, star = (lid // 100) % 100, lid % 100
    name = _MAJOR_NAMES.get(major)
    if not name or star < 1:
        return '未知段位'
    return f'{name}Lv{star}' if major >= 6 else f'{name}{star}星'


# ---------------------------------------------------------------- 拉取

async def _fetch_records(player_num: PlayerNum, player_id: int, start_time: datetime,
                         end_time: datetime, room_rank: AbstractSet[RoomRank],
                         limit: int) -> list[GameRecord]:
    """按需分页拉取最近的 `limit` 条记录（新→旧），需要牌谱屋 Key。"""
    api_key = normalize_api_key(conf.majsoul_paifuya_api_key)
    if not api_key:
        raise QueryError(RECORD_KEY_REQUIRED)
    records: list[GameRecord] = []
    cursor = end_time
    try:
        while len(records) < limit and cursor >= start_time:
            batch = min(RECORD_PAGE_SIZE, limit - len(records))
            try:
                page = await api[player_num].player_records(
                    player_id, start_time, cursor, room_rank,
                    limit=batch, descending=True, api_key=api_key,
                )
            except HTTPStatusError as exc:
                if records and exc.response.status_code == 404:
                    break
                raise
            records.extend(page[:batch])
            if len(page) < batch or len(records) >= limit:
                break
            next_cursor = page[-1].start_time - timedelta(seconds=1)
            if next_cursor >= cursor:
                break
            cursor = next_cursor
    except (HTTPError, PaifuyaRateLimitError) as exc:
        logger.warning('Paifuya record request failed (%s)', type(exc).__name__)
        raise
    return records[:limit]


_MODE_NAMES = {8: '金东', 9: '金南', 11: '玉东', 12: '玉南', 15: '王座东', 16: '王座南',
               21: '三麻金东', 22: '三麻金南', 23: '三麻玉东', 24: '三麻玉南',
               25: '三麻王座东', 26: '三麻王座南'}
_MODE_PRIORITY = (12, 24, 9, 22, 16, 26, 11, 23, 8, 21)   # 玉南 → 金南 → 王座南 → 东场（样本量从大到小）


async def _peer_baseline(player_num: PlayerNum, room_rank: AbstractSet[RoomRank],
                         level_id: int) -> tuple[Optional[dict], str]:
    """取同段位（同房间模式）的均值数据，返回 `(peer, 模式名)`。

    模式按样本量优先级挑（半庄优先），因为段位均值在小样本模式里噪音大。
    拿不到返回 `(None, '')`——只少一块对比，不影响主图。
    """
    modes = {int(r.value) for r in room_rank}
    if not modes:
        return None, ''
    mode = next((m for m in _MODE_PRIORITY if m in modes), sorted(modes)[0])
    label = _MODE_NAMES.get(mode, '')
    now = datetime.now(timezone.utc).timestamp()
    hit = _peer_cache.get(mode)
    if hit and now - hit[0] < _PEER_TTL:
        data = hit[1]
    else:
        try:
            data = await api[player_num].global_statistics(mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning('获取段位均值失败（%s），跳过指标对比', type(exc).__name__)
            data = {}
        _peer_cache[mode] = (now, data or {})

    levels = (data or {}).get(str(mode)) or {}
    if not levels:
        return None, label
    key = str(int(level_id or 0))
    if key in levels:
        return levels[key], label
    major = (int(level_id or 0) // 100) % 100
    same_major = [v for k, v in levels.items()
                  if k.isdigit() and (int(k) // 100) % 100 == major]
    pool = same_major or list(levels.values())
    return (max(pool, key=lambda v: int(v.get('num_players') or 0)) if pool else None), label


# ---------------------------------------------------------------- 序列

def _init_score(level_id: int) -> int:
    """段位初始分 = 该段位上限的一半（雀魂规则）。"""
    try:
        return max(1, PlayerRank.from_code(int(level_id)).max_pt // 2)
    except Exception:  # noqa: BLE001
        return 0


def _build_series(records: list[GameRecord], player_id: int, end_score: int = 0) -> dict:
    """记录（旧→新）→ 局后段位分序列 + 顺位序列 + 跨段分界。

    牌谱屋每局只给「本局结算前的段位」与「本局段位分变动」，不给局前分。这里**从末尾倒推**：
    末局局后分用 `search_player` 的官方当前分（`end_score`）锚定，往前逐局减去本局变动；
    遇到段位变化（本局段位 ≠ 上一局段位，说明那一局发生了升/降段）则改取新段位初始分，
    这样曲线末端与面板当前分严格一致，跨段处的台阶也落在正确位置。
    官方分缺失时退化为「从首局段位初始分正向累加」。
    """
    rows: list[dict] = []
    seq: list[int] = []
    for rec in records:
        focus = next((p for p in rec.players if p.id == player_id), None)
        if focus is None:
            continue
        ordered = sorted(rec.players, key=lambda p: -int(p.score))
        rank = next((i + 1 for i, p in enumerate(ordered) if p.id == player_id), 0)
        rows.append({'level_id': int(focus.rank.code), 'pt': int(focus.pt)})
        seq.append(rank)

    changes: list[dict] = []
    for i in range(1, len(rows)):
        if rows[i]['level_id'] != rows[i - 1]['level_id']:
            changes.append({'index': i, 'level_id': rows[i]['level_id'],
                            'name': level_name(rows[i]['level_id'])})

    scores: list[int] = [0] * len(rows)
    if rows:
        if end_score:
            scores[-1] = int(end_score)
            for i in range(len(rows) - 1, 0, -1):
                if rows[i]['level_id'] != rows[i - 1]['level_id']:
                    scores[i - 1] = _init_score(rows[i]['level_id'])
                else:
                    scores[i - 1] = scores[i] - rows[i]['pt']
        else:                                  # 无官方分：按首局段位初始分正向累加
            cur = _init_score(rows[0]['level_id'])
            for i, row in enumerate(rows):
                if i and row['level_id'] != rows[i - 1]['level_id']:
                    cur = _init_score(row['level_id'])
                cur += row['pt']
                scores[i] = cur

    return {
        'level_scores': scores,
        'rank_changes': changes,
        'seq': seq,
        'last_level_id': rows[-1]['level_id'] if rows else 0,
        'pt_total': sum(r['pt'] for r in rows),
    }


def _summary(seq: list[int], four: bool, pt_total: int) -> dict:
    """战绩摘要；「段位分变化」用窗口内 PT 净和（跨段时首末分差没有意义）。"""
    n = len(seq)
    if not n:
        return {}
    last_rank = 4 if four else 3
    best = streak = 0
    for r in seq:
        streak = streak + 1 if r == 1 else 0
        best = max(best, streak)
    return {
        'games': n,
        'avg_rank': round(sum(seq) / n, 2),
        'win_rate': round(sum(1 for r in seq if r == 1) / n, 4),
        'top2_rate': round(sum(1 for r in seq if r <= 2) / n, 4),
        'last_rate': round(sum(1 for r in seq if r == last_rank) / n, 4),
        'best_streak': best,
        'level_delta': int(pt_total),
    }


# ---------------------------------------------------------------- 指标对比 / 雷达

_METRICS = (
    ('和牌率', '和牌率', 'pct', True),
    ('放铳率', '放铳率', 'pct', False),
    ('副露率', '副露率', 'pct', None),
    ('立直率', '立直率', 'pct', None),
    ('平均打点', '平均打点', 'int', True),
    ('平均铳点', '平均铳点', 'int', False),
    ('净打点效率', '净打点效率', 'int', True),
    ('和了巡数', '和了巡数', 'dec', False),
    ('立直巡目', '立直巡目', 'dec', False),
    ('先制率', '先制率', 'pct', True),
    ('立直后和牌率', '立直后和牌率', 'pct', True),
    ('副露后和牌率', '副露后和牌率', 'pct', True),
)

_AXES = (
    ('进攻', (('和牌率', True), ('副露后和牌率', True))),
    ('防守', (('放铳率', False), ('平均铳点', False))),
    ('副露', (('副露率', None),)),
    ('打点', (('平均打点', True), ('净打点效率', True))),
    ('速度', (('和了巡数', False), ('立直巡目', False))),
)


def _fmt(value, kind: str) -> str:
    if value is None:
        return ''
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ''
    if kind == 'pct':
        return f'{v * 100:.1f}%'
    if kind == 'int':
        return f'{v:,.0f}'
    return f'{v:.2f}'


def _metrics(ext: dict, peer_ext: dict) -> list[dict]:
    rows = []
    for key, label, kind, higher_better in _METRICS:
        if key not in ext:
            continue
        mine, base = ext.get(key), peer_ext.get(key)
        better = None
        if higher_better is not None and isinstance(base, (int, float)) \
                and isinstance(mine, (int, float)) and base:
            better = mine > base if higher_better else mine < base
        rows.append({'label': label, 'value': _fmt(mine, kind),
                     'avg': _fmt(base, kind), 'better': better})
    return rows


def _radar(ext: dict, peer_ext: dict) -> list[float]:
    """牌风分（50 = 同段位均值）；无法计算的维度按 50。"""
    out = []
    for _name, parts in _AXES:
        scores = []
        for key, higher_better in parts:
            mine, base = ext.get(key), peer_ext.get(key)
            if not isinstance(mine, (int, float)) or not isinstance(base, (int, float)) or not base:
                continue
            if mine == base:
                scores.append(50.0)
                continue
            if higher_better is False:
                ratio = float(base) / float(mine) if mine else 2.0
            else:
                ratio = float(mine) / float(base)
            scores.append(max(0.0, min(100.0, 50.0 + (ratio - 1.0) * 50.0)))
        out.append(round(sum(scores) / len(scores), 1) if scores else 50.0)
    return out


# ---------------------------------------------------------------- 头像

async def _qq_avatar(qq: str) -> Optional[bytes]:
    qq = str(qq or '').strip()
    if not qq.isdigit():
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            resp = await client.get(f'https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640')
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.content
    except Exception:  # noqa: BLE001 —— 头像只是装饰
        return None
    return None


# ---------------------------------------------------------------- 主流程

async def handle_majsoul_record(emit, nickname: str, player_num: PlayerNum, *,
                                room_rank: Optional[AbstractSet[RoomRank]] = None,
                                start_time: Optional[datetime] = None,
                                end_time: Optional[datetime] = None,
                                limit: Optional[int] = None,
                                qq: str = '') -> None:
    """拉取记录并推送「战绩走势卡」；渲染失败自动回退纯文本。"""
    if room_rank is None:
        room_rank = all_four_player_room_rank if player_num == PlayerNum.four \
            else all_three_player_room_rank
    if start_time is None:
        start_time = datetime(2010, 1, 1, tzinfo=timezone.utc)
    if end_time is None:
        end_time = datetime.now(timezone.utc)
    cap = max(1, int(conf.majsoul_record_max_games))
    want = min(int(limit or cap), cap)
    four = player_num == PlayerNum.four

    players = await api[player_num].search_player(nickname)
    players = [p for p in players if p.nickname == nickname]
    if not players:
        raise QueryError('没有查询到该角色在金之间以上的对局数据呢~')

    avatar = await _qq_avatar(qq)
    sent = False
    for player in players:
        try:
            records = await _fetch_records(player_num, player.id, start_time, end_time,
                                           room_rank, want)
        except HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue
            raise
        if not records:
            continue

        records = list(reversed(records))                 # 旧 → 新，画曲线用

        level = fix_level(player.level)                   # 修正「刚升/降段但 delta 未同步」
        cur_level_id = int(level.id.code)
        official = int(level.score)
        series = _build_series(records, player.id, official)
        seq, scores = series['seq'], list(series['level_scores'])
        if not seq:
            continue

        try:
            level_next = int(PlayerRank.from_code(cur_level_id).max_pt)
        except Exception:  # noqa: BLE001
            level_next = 0

        payload = {
            'nickname': player.nickname,
            'level_name': level_name(cur_level_id),
            'level_id': cur_level_id,
            'level_score': official,
            'level_next': level_next,
            'four': four,
            'scope': f'{"四麻" if four else "三麻"} · 最近 {len(seq)} 局',
            'seq': seq,
            'level_scores': scores,
            'rank_changes': series['rank_changes'],
            'pt_total': series['pt_total'],
            'summary': _summary(seq, four, series['pt_total']),
            'avatar': avatar,
            'theme': str(conf.majsoul_record_theme or 'dark'),
            'detail': str(conf.majsoul_record_detail or 'full'),
        }

        # ★ 派生统计：逐局流水 / 连打分段 / 时段 / 稳定度 / 素点
        #   纯本地计算，不额外请求；任何异常都不该影响主图。
        try:
            rows = record_analytics.game_rows(records, player.id)
            sessions = record_analytics.find_sessions(rows)
            extra = record_analytics.summarize(rows, sessions, four)
            if extra:
                payload['game_rows'] = rows
                payload['sessions'] = sessions
                payload['hours'] = extra.pop('hours', [])
                payload['summary'].update(extra)
        except Exception as exc:  # noqa: BLE001
            logger.warning('战绩派生统计失败（不影响主图）：%s', exc)

        if conf.majsoul_record_compare:
            try:
                ext = await api[player_num].player_extended_stats(
                    player.id, start_time, end_time, room_rank)
                peer, peer_mode = await _peer_baseline(player_num, room_rank, cur_level_id)
                peer_ext = (peer or {}).get('extended') or {}
                mine = ext.model_dump(exclude_none=True)
                if peer_ext:
                    scope = f'{level_name(cur_level_id)}' + (f' {peer_mode}' if peer_mode else '')
                    payload['metrics'] = _metrics(mine, peer_ext)
                    payload['radar'] = _radar(mine, peer_ext)
                    payload['radar_base'] = scope
                    payload['metrics_scope'] = f'{scope} 段位均值'
            except (HTTPError, PaifuyaRateLimitError):
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning('战绩卡指标对比生成失败：%s', exc)

        png = None
        try:
            png = await _render(payload)
        except Exception as exc:  # noqa: BLE001 —— 渲染失败只发文字，不让命令失败
            logger.warning('战绩卡渲染失败，回退纯文本：%s', exc)
        await _emit(emit, _summary_text(payload), png)
        sent = True

        # ★ 战绩卡发完之后，再追加一张 AI 锐评卡（独立失败，不回滚已发的战绩卡）
        await _emit_ai_comment(emit, payload)

    if not sent:
        raise QueryError('该账号在所选范围内没有对局记录')


async def _render(payload: dict) -> Optional[bytes]:
    from asyncio import to_thread
    if not record_card.available():
        return None
    return await to_thread(record_card.render_record, payload)


def _summary_text(p: dict) -> str:
    s = p.get('summary') or {}
    head = f"昵称：{p.get('nickname')} · {p.get('level_name', '')} · 最近 {s.get('games', 0)} 局"
    tail = []
    if s.get('avg_rank'):
        tail.append(f"平均顺位 {s['avg_rank']:.2f}")
    if s.get('win_rate') is not None:
        tail.append(f"一位率 {s['win_rate'] * 100:.0f}%")
    if s.get('top2_rate') is not None:
        tail.append(f"连对率 {s['top2_rate'] * 100:.0f}%")
    if s.get('best_streak'):
        tail.append(f"最长连一位 {s['best_streak']}")
    if s.get('level_delta'):
        tail.append(f"段位分 {int(s['level_delta']):+d}")
    return '\n'.join(x for x in (head, ' · '.join(tail)) if x)


async def _emit(emit, text: Optional[str], png: Optional[bytes]) -> None:
    from astrbot.api.message_components import Plain as Text, Image as AstrImage
    chain = []
    if text:
        chain.append(Text(text))
    if png:
        chain.append(AstrImage.fromBytes(png))
    if chain:
        await emit(chain)


# ---------------------------------------------------------------- AI 锐评卡

def _comment_chips(p: dict) -> list[str]:
    """锐评卡顶部的小标签：只放最有信息量的几个数，放不下由绘制端自动截断。"""
    s = p.get('summary') or {}
    out = []
    if s.get('avg_rank'):
        out.append(f"平均顺位 {float(s['avg_rank']):.2f}")
    if s.get('win_rate') is not None:
        out.append(f"一位率 {float(s['win_rate']) * 100:.0f}%")
    if s.get('last_rate') is not None:
        out.append(f"四位率 {float(s['last_rate']) * 100:.0f}%")
    if s.get('best_streak'):
        out.append(f"最长连一位 {int(s['best_streak'])}")
    if s.get('level_delta'):
        out.append(f"段位分 {int(s['level_delta']):+d}")
    return out


def _comment_facts(p: dict) -> str:
    """给模型的纯文本事实（不带图），口径与卡片一致。"""
    s = p.get('summary') or {}
    lines = [f"昵称：{p.get('nickname', '')}",
             f"段位：{p.get('level_name', '')}（当前 {p.get('level_score', 0)} 分）",
             f"统计范围：{p.get('scope', '')}"]
    pairs = (('对局数', 'games'), ('平均顺位', 'avg_rank'), ('一位率', 'win_rate'),
             ('连对率', 'top2_rate'), ('四位率', 'last_rate'), ('最长连一位', 'best_streak'))
    for label, key in pairs:
        v = s.get(key)
        if v is None:
            continue
        if key in ('win_rate', 'top2_rate', 'last_rate'):
            lines.append(f"{label}：{float(v) * 100:.1f}%")
        else:
            lines.append(f"{label}：{v}")
    if s.get('level_delta') is not None:
        lines.append(f"段位分净变化：{int(s['level_delta']):+d}")
    seq = p.get('seq') or []
    if seq:
        lines.append('最近顺位（旧→新）：' + ' '.join(str(r) for r in seq[-30:]))
    metrics = p.get('metrics') or []
    if metrics:
        rows = []
        for m in metrics:
            row = f"{m.get('label')} {m.get('value')}"
            if m.get('avg'):
                row += f"（同段位均值 {m['avg']}）"
            better = m.get('better')
            row += '，优于均值' if better is True else ('，差于均值' if better is False else '')
            rows.append(row)
        lines.append('关键指标：' + '；'.join(rows))
    radar = p.get('radar') or []
    if len(radar) == 5:
        axes = ('进攻', '防守', '副露', '打点', '速度')
        lines.append('牌风评分（50=同段位均值）：'
                     + '、'.join(f'{a}{float(v):.0f}' for a, v in zip(axes, radar)))
    return '\n'.join(lines)


async def _emit_ai_comment(emit, p: dict) -> None:
    """在战绩卡之后追加 AI 锐评卡；任何失败都只补一句提示，不抛异常。"""
    if not conf.majsoul_record_ai_comment or not conf.ai_comment_enabled:
        return
    from .ai_comment import generate_ai_comment
    try:
        text = (await generate_ai_comment(_comment_facts(p)) or '').strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning('战绩 AI 锐评生成失败：%s', exc)
        await _emit(emit, f'AI 锐评生成失败：{exc}', None)
        return
    if not text:
        return

    card = dict(p)
    card.update({'text': text, 'chips': _comment_chips(p),
                 'model': str(conf.majsoul_ai_model or ''),
                 'scope': p.get('scope', '')})
    png = None
    try:
        png = await _render_comment(card)
    except Exception as exc:  # noqa: BLE001 —— 画不出来就发纯文本，别把锐评丢掉
        logger.warning('AI 锐评卡渲染失败，回退纯文本：%s', exc)
    await _emit(emit, None if png else text, png)


async def _render_comment(payload: dict) -> Optional[bytes]:
    from asyncio import to_thread
    if not record_card.available():
        return None
    return await to_thread(record_card.render_comment, payload)