from asyncio import Semaphore, sleep
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import partial
from time import monotonic
import math
from typing import List, AbstractSet, AsyncGenerator, Optional, Tuple

from httpx import AsyncClient, URL, HTTPError, HTTPStatusError, Response
from astrbot.api import logger

from ...errors import PaifuyaRateLimitError
from ...config import conf, normalize_api_key
from ...network.auto_retry import auto_retry
from ...network.host_prober import HostProber
from .models.game_record import GameRecord
from .models.player_extended_stats import PlayerExtendedStats
from .models.player_info import PlayerInfo
from .models.player_num import PlayerNum
from .models.player_stats import PlayerStats
from .models.room_rank import RoomRank

prober = HostProber([
    # Keep this aligned with the mirrors used by the official web frontend.
    "5-data.amae-koromo.com",
    "1.data.amae-koromo.com",
    "2.data.amae-koromo.com",
    "4.data.amae-koromo.com",
])

_REQUEST_INTERVAL = 1.10  # Stay safely below the provider's maximum 1 QPS.
_request_semaphore = Semaphore(1)
_next_request_at = 0.0

# Identify the command sender, never the queried game nickname or API key.
_request_user: ContextVar[Optional[str]] = ContextVar('paifuya_request_user', default=None)
_cooldowns_by_user: dict[str, tuple[float, PaifuyaRateLimitError]] = {}
_RETRYABLE_PAIFUYA_ERRORS = (HTTPError, PaifuyaRateLimitError)


@contextmanager
def paifuya_user(user_id: str):
    token = _request_user.set(user_id)
    try:
        yield
    finally:
        _request_user.reset(token)


def _is_retryable_http_error(error: Exception) -> bool:
    if isinstance(error, PaifuyaRateLimitError):
        return False
    if not isinstance(error, HTTPStatusError):
        return True

    status_code = error.response.status_code
    return status_code in (408, 502, 503, 504)


def _http_retry_delay(error: Exception, retry_number: int) -> float:
    return min(2 ** (retry_number - 1), 4)


class PaifuyaApi:
    def __init__(self, baseurl: str, player_num: PlayerNum):
        self._baseurl = baseurl
        self.player_num = player_num

        async def req_hook(request):
            logger.debug(f"Request: {request.method} {request.url} - Waiting for response")

        async def resp_hook(response):
            request = response.request
            logger.debug(f"Response: {request.method} {request.url} - Status {response.status_code}")

            if response.status_code in (401, 403, 429):
                try:
                    await response.aread()
                    response_body = response.text[:500]
                except Exception as e:
                    response_body = f"<failed to read response body: {type(e).__name__}>"

                delay = conf.majsoul_paifuya_cooldown_seconds
                error = PaifuyaRateLimitError(
                    retry_after=delay,
                    auth_required=response.status_code in (401, 403) or 'x-cap-token-required' in response_body,
                    auth_sent=bool(request.headers.get('Authorization')),
                )
                user_id = _request_user.get()
                if user_id is not None:
                    _cooldowns_by_user[user_id] = (monotonic() + delay, error)
                logger.warning('Paifuya rejected request: status=%s; credential_sent=%s; retry_after=%s; authorization_required=%s',
                               response.status_code, error.auth_sent, delay, error.auth_required)
                raise error

            response.raise_for_status()

        self.client: AsyncClient = AsyncClient(
            follow_redirects=True,
            event_hooks={'request': [req_hook], 'response': [resp_hook]}
        )


    async def close(self):
        await self.client.aclose()

    async def _get(self, url: URL, *, params, headers=None):
        global _next_request_at

        key = normalize_api_key(conf.majsoul_paifuya_api_key)
        request_headers = {'Authorization': f'Bearer {key}'} if key else {}
        request_headers.update(headers or {})
        async with _request_semaphore:
            now = monotonic()
            for user_id, (until, _) in list(_cooldowns_by_user.items()):
                if now >= until:
                    _cooldowns_by_user.pop(user_id, None)
            blocked = _cooldowns_by_user.get(_request_user.get())
            if blocked is not None:
                until, error = blocked
                raise PaifuyaRateLimitError(
                    retry_after=math.ceil(until - now),
                    auth_required=error.auth_required,
                    auth_sent=error.auth_sent,
                )
            wait_time = _next_request_at - monotonic()
            if wait_time > 0:
                await sleep(wait_time)

            _next_request_at = monotonic() + _REQUEST_INTERVAL
            return await self.client.get(url, params=params, headers=request_headers)

    @auto_retry(
        _RETRYABLE_PAIFUYA_ERRORS,
        before_retry=partial(prober.select_host, exclude_current=True),
        attempts=4,
        retry_if=_is_retryable_http_error,
        retry_delay=_http_retry_delay,
    )
    async def search_player(
            self, nickname: str,
            *, limit: int = 10
    ) -> List[PlayerInfo]:
        resp = await self._get(
            URL(f"https://{prober.host}/{self._baseurl}/search_player/{nickname}"),
            params={"limit": limit}
        )
        players = [PlayerInfo.parse_obj(x) for x in resp.json()]
        players = [
            p for p in players
            if p.level.id.player_num == self.player_num
            and p.nickname == nickname
        ]
        return players

    @auto_retry(
        _RETRYABLE_PAIFUYA_ERRORS,
        before_retry=partial(prober.select_host, exclude_current=True),
        attempts=4,
        retry_if=_is_retryable_http_error,
        retry_delay=_http_retry_delay,
    )
    async def player_stats(
            self, player_id: int, start_time: datetime, end_time: datetime, room_rank: AbstractSet[RoomRank]
    ) -> PlayerStats:
        start_timestamp = int(start_time.timestamp() * 1000)
        end_timestamp = int(end_time.timestamp() * 1000)
        mode = ".".join(map(lambda x: str(x.value), room_rank))
        resp = await self._get(
            URL(f"https://{prober.host}/{self._baseurl}/player_stats/{player_id}/{start_timestamp}/{end_timestamp}"),
            params={"mode": mode}
        )
        return PlayerStats.parse_obj(resp.json())

    async def player_stats_for_recent_games(
            self, player_id: int, start_time: datetime, end_time: datetime,
            room_rank: AbstractSet[RoomRank], *, limit: int,
            latest_time: Optional[datetime] = None
    ) -> Tuple[datetime, Optional[PlayerStats]]:
        """Find an exact recent-game range using aggregate stats only.

        ``player_stats`` does not support a game-count limit. Its returned
        ``count`` is monotonic as the range start moves forward, so the plugin
        can locate a range containing exactly ``limit`` games without calling
        the CAPTCHA-protected ``player_records`` endpoint.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")

        range_start = int(start_time.timestamp())
        range_end = int(end_time.timestamp())
        if range_start >= range_end:
            return start_time, await self.player_stats(
                player_id, start_time, end_time, room_rank
            )

        if latest_time is not None:
            latest_end = int((latest_time + timedelta(seconds=1)).timestamp())
            search_end = min(range_end, max(range_start, latest_end))
        else:
            search_end = range_end

        cache: dict[int, Optional[PlayerStats]] = {}

        def to_datetime(timestamp: int) -> datetime:
            return datetime.fromtimestamp(timestamp, tz=start_time.tzinfo)

        async def probe(timestamp: int) -> Optional[PlayerStats]:
            if timestamp not in cache:
                try:
                    cache[timestamp] = await self.player_stats(
                        player_id,
                        to_datetime(timestamp),
                        end_time,
                        room_rank,
                    )
                except HTTPStatusError as e:
                    if e.response.status_code != 404:
                        raise
                    cache[timestamp] = None
            return cache[timestamp]

        def game_count(stats: Optional[PlayerStats]) -> int:
            return stats.count if stats is not None else 0

        # Expand backwards from the latest known game. For active players this
        # usually brackets the requested count in only a few requests.
        upper = search_end
        window = 24 * 60 * 60
        while True:
            lower = max(range_start, search_end - window)
            lower_stats = await probe(lower)
            lower_count = game_count(lower_stats)

            if lower_count == limit:
                return to_datetime(lower), lower_stats
            if lower_count > limit:
                break
            if lower == range_start:
                # Match the old behavior when fewer than ``limit`` games are
                # available: return statistics for every available game.
                return start_time, lower_stats

            upper = lower
            window *= 2

        # ``lower`` contains too many games and ``upper`` too few. Locate a
        # timestamp whose aggregate contains the exact requested count.
        while lower + 1 < upper:
            middle = (lower + upper) // 2
            middle_stats = await probe(middle)
            middle_count = game_count(middle_stats)

            if middle_count == limit:
                return to_datetime(middle), middle_stats
            if middle_count > limit:
                lower = middle
            else:
                upper = middle

        raise RuntimeError(
            f"unable to locate an exact range containing {limit} games"
        )

    @auto_retry(
        _RETRYABLE_PAIFUYA_ERRORS,
        before_retry=partial(prober.select_host, exclude_current=True),
        attempts=4,
        retry_if=_is_retryable_http_error,
        retry_delay=_http_retry_delay,
    )
    async def player_extended_stats(
            self, player_id: int, start_time: datetime, end_time: datetime, room_rank: AbstractSet[RoomRank]
    ) -> PlayerExtendedStats:
        start_timestamp = int(start_time.timestamp() * 1000)
        end_timestamp = int(end_time.timestamp() * 1000)
        mode = ".".join(map(lambda x: str(x.value), room_rank))
        resp = await self._get(
            URL(f"https://{prober.host}/{self._baseurl}/player_extended_stats/{player_id}/{start_timestamp}/{end_timestamp}"),
            params={"mode": mode}
        )
        return PlayerExtendedStats.parse_obj(resp.json())

    @auto_retry(
        _RETRYABLE_PAIFUYA_ERRORS,
        before_retry=partial(prober.select_host, exclude_current=True),
        attempts=4,
        retry_if=_is_retryable_http_error,
        retry_delay=_http_retry_delay,
    )
    async def player_records(
            self, player_id: int, start_time: datetime, end_time: datetime, room_rank: AbstractSet[RoomRank],
            *, limit: int, descending: bool = True,
            api_key: Optional[str] = None
    ) -> List[GameRecord]:
        start_timestamp = int(start_time.timestamp() * 1000)
        end_timestamp = int(end_time.timestamp() * 1000)
        mode = ".".join(map(lambda x: str(x.value), room_rank))
        api_key = normalize_api_key(api_key or '')
        headers = (
            {"Authorization": f"Bearer {api_key}"}
            if api_key
            else None
        )
        resp = await self._get(
            URL(f"https://{prober.host}/{self._baseurl}/player_records/{player_id}/{end_timestamp}/{start_timestamp}"),
            params={"mode": mode, "limit": str(limit), "descending": str(descending).lower()},
            headers=headers,
        )
        return [GameRecord.parse_obj(x) for x in resp.json()]

    async def player_records_stream(
            self, player_id: int, start_time: datetime, end_time: datetime, room_rank: AbstractSet[RoomRank],
            *, batch: int = 200, descending: bool = True,
            api_key: Optional[str] = None
    ) -> AsyncGenerator[GameRecord, None]:
        while start_time <= end_time:
            try:
                records = await self.player_records(
                    player_id, start_time, end_time, room_rank,
                    limit=batch,
                    descending=descending,
                    api_key=api_key,
                )
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    break
                raise

            for r in records:
                yield r

            if len(records) < batch:
                break

            end_time = records[-1].start_time - timedelta(seconds=1)
            
    async def search_player_by_uid(self, uid: int):
        resp = await self._get(
            URL(f"https://{prober.host}/{self._baseurl}/player_stats/{uid}"),
            params={}
        )
        return resp.json()


four_player_api = PaifuyaApi(f"api/v2/pl4", PlayerNum.four)
three_player_api = PaifuyaApi(f"api/v2/pl3", PlayerNum.three)

paifuya_api = {
    PlayerNum.four: four_player_api,
    PlayerNum.three: three_player_api,
}

__all__ = ("PaifuyaApi", "four_player_api", "three_player_api", "paifuya_api")
