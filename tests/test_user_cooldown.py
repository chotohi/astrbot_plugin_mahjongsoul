import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from conftest import Event, collect
from test_auth import mock_api, no_delay
from astrbot_plugin_mahjongsoul.config import Config, conf
from astrbot_plugin_mahjongsoul.errors import PaifuyaRateLimitError
from astrbot_plugin_mahjongsoul.main import Majsoul
from astrbot_plugin_mahjongsoul.paifuya import query_majsoul_pt_plot as plot
from astrbot_plugin_mahjongsoul.paifuya.data import api


def test_cooldown_config_defaults_and_validation():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / '_conf_schema.json').read_text('utf-8'))
    reference = json.loads((root / 'config.reference.json').read_text('utf-8'))
    key = 'majsoul_paifuya_cooldown_seconds'
    assert Config().majsoul_paifuya_cooldown_seconds == schema[key]['default'] == 3600
    assert reference['properties'][key] == Config.model_json_schema()['properties'][key]
    assert Config(**{key: 120}).majsoul_paifuya_cooldown_seconds == 120
    for invalid in (0, -1, 1.5, 'invalid'):
        with pytest.raises(ValidationError):
            Config(**{key: invalid})


@pytest.mark.parametrize('delay', [3600, 120])
async def test_qq_users_have_independent_deadlines(context, monkeypatch, no_delay, delay):
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-key')
    monkeypatch.setattr(conf, 'majsoul_paifuya_cooldown_seconds', delay)
    clock = [100.0]
    monkeypatch.setattr(api, 'monotonic', lambda: clock[0])
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) <= 2:
            return httpx.Response(429, headers={'Retry-After': '86400'})
        return httpx.Response(200, json=[])

    client = await mock_api(handler)

    async def query(emit, nickname, player_num, **kwargs):
        await client.search_player(nickname)

    monkeypatch.setattr(plot, 'handle_majsoul_pt_plot', query)
    plugin = Majsoul(context, {})
    plugin.bindings = SimpleNamespace(data={})
    plugin.query_lock = asyncio.Lock()
    try:
        result = await collect(plugin.query(Event('/雀魂PT图 shared-name', user='101')))
        assert f'等待 {delay} 秒' in result[0].chain[0].text
        assert len(requests) == 1

        clock[0] += 10
        # A different group, nickname, and three-player alias still belong to A.
        result = await collect(plugin.query(Event('/雀魂三麻PT走向查询 other-name', user='101', group='202')))
        assert f'等待 {delay - 10} 秒' in result[0].chain[0].text
        assert len(requests) == 1

        clock[0] += 10
        # B can request the very same game nickname using the same API key.
        result = await collect(plugin.query(Event('/雀魂PT图 shared-name', user='102')))
        assert f'等待 {delay} 秒' in result[0].chain[0].text
        assert len(requests) == 2

        clock[0] = 100 + delay
        # A expires exactly on its own deadline despite B's more recent error.
        assert await collect(plugin.query(Event('/雀魂PT图 shared-name', user='101', group=''))) == []
        assert len(requests) == 3
        result = await collect(plugin.query(Event('/雀魂PT图 shared-name', user='102')))
        assert '等待 20 秒' in result[0].chain[0].text
        assert len(requests) == 3

        clock[0] += 20
        assert await collect(plugin.query(Event('/雀魂PT图 shared-name', user='102'))) == []
        assert len(requests) == 4
        assert not api._cooldowns_by_user
        assert api._request_user.get() is None
    finally:
        await client.close()


async def test_concurrent_user_context_is_isolated(monkeypatch, no_delay):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    client = await mock_api(handler)

    async def query(user):
        with api.paifuya_user(user):
            await asyncio.sleep(0)
            for _ in range(2):
                with pytest.raises(PaifuyaRateLimitError):
                    await client.search_player('same-name')

    try:
        await asyncio.gather(query('test:101'), query('test:102'))
        assert len(calls) == 2
        assert api._request_user.get() is None
    finally:
        await client.close()


async def test_query_timeout_resets_user_context(context, monkeypatch):
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-key')

    async def timeout(*args, **kwargs):
        assert api._request_user.get() == 'test:101'
        raise TimeoutError

    monkeypatch.setattr(plot, 'handle_majsoul_pt_plot', timeout)
    plugin = Majsoul(context, {})
    plugin.query_lock = asyncio.Lock()
    result = await collect(plugin.query(Event('/雀魂PT图 nickname')))
    assert '超时' in result[0].chain[0].text
    assert api._request_user.get() is None
