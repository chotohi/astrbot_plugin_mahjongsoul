import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import Event, collect
from astrbot_plugin_mahjongsoul.config import conf
from astrbot_plugin_mahjongsoul.errors import PaifuyaRateLimitError
from astrbot_plugin_mahjongsoul.paifuya.data import api as module
from astrbot_plugin_mahjongsoul.paifuya.data.models.player_num import PlayerNum


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(module, '_REQUEST_INTERVAL', 0)
    monkeypatch.setattr(module, '_next_request_at', 0)
    monkeypatch.setattr(module, '_request_semaphore', asyncio.Semaphore(1))


async def mock_api(handler):
    api = module.PaifuyaApi('api/v2/pl3', PlayerNum.three)
    hooks = api.client.event_hooks
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks=hooks)
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['dummy-secret', ' Bearer dummy-secret ', 'bearer dummy-secret'])
async def test_configured_key_is_sent_for_search_and_records(monkeypatch, no_delay, key):
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', key)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])
    api = await mock_api(handler)
    try:
        await api.search_player('test')
        now = datetime.now(timezone.utc)
        await api.player_records(123, now, now, set(), limit=500)
        assert len(requests) == 2
        assert all(r.headers['Authorization'] == 'Bearer dummy-secret' for r in requests)
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(429, '[x-cap-token-required] CAPTCHA required'), (429, 'rate limited'), (401, 'unauthorized'), (403, 'forbidden')])
async def test_rejections_stop_retry_and_enforce_cooldown(monkeypatch, no_delay, caplog, status, body):
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-secret')
    monkeypatch.setattr(module, 'monotonic', lambda: 100.0)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=body, headers={'Retry-After': '86400'})
    api = await mock_api(handler)
    try:
        for _ in range(2):
            with module.paifuya_user('test:101'), pytest.raises(PaifuyaRateLimitError) as caught:
                await api.search_player('test')
            assert caught.value.auth_sent and caught.value.retry_after == 3600
            assert '已携带' in str(caught.value)
        assert len(requests) == 1
        assert 'dummy-secret' not in caplog.text and 'dummy-secret' not in str(caught.value)
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_pt_auth_failure_does_not_fall_back_anonymously(monkeypatch):
    from astrbot_plugin_mahjongsoul.paifuya import query_majsoul_pt_plot as plot
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-secret')
    player = SimpleNamespace(id=123, nickname='test')
    error = PaifuyaRateLimitError(auth_required=True, auth_sent=True)
    fake = SimpleNamespace(search_player=AsyncMock(return_value=[player]),
                           player_records=AsyncMock(side_effect=error),
                           player_records_stream=AsyncMock())
    monkeypatch.setitem(plot.api, PlayerNum.three, fake)
    with pytest.raises(PaifuyaRateLimitError):
        await plot.handle_majsoul_pt_plot(AsyncMock(), 'test', PlayerNum.three)
    fake.player_records.assert_awaited_once()
    fake.player_records_stream.assert_not_called()


@pytest.mark.asyncio
async def test_status_only_reports_loaded_state(context, monkeypatch):
    from astrbot_plugin_mahjongsoul.main import Majsoul
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-secret')
    plugin = Majsoul(context, {})
    result = await collect(plugin.api_status(Event('/雀魂接口状态', admin=True)))
    assert '已加载' in result[0].chain[0].text
    assert 'dummy-secret' not in result[0].chain[0].text
    denied = await collect(plugin.api_status(Event('/雀魂接口状态')))
    assert '管理员权限' in denied[0].chain[0].text
