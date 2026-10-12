import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from openai import AuthenticationError, PermissionDeniedError

from conftest import Event, collect
from astrbot_plugin_mahjongsoul.config import conf
from astrbot_plugin_mahjongsoul.errors import QueryError, PaifuyaRateLimitError
from astrbot_plugin_mahjongsoul.main import Majsoul
from astrbot_plugin_mahjongsoul.paifuya import ai_comment, query_majsoul_pt_plot as plot
from astrbot_plugin_mahjongsoul.paifuya.data import api
from astrbot_plugin_mahjongsoul.paifuya.data.models.player_num import PlayerNum


@pytest.mark.parametrize('key', ['', '   ', 'Bearer ', 'bearer'])
async def test_missing_ai_key_never_creates_client(monkeypatch, key):
    monkeypatch.setattr(conf, 'majsoul_ai_api_key', key)
    factory = Mock(side_effect=AssertionError('Must not contact AI without a key'))
    monkeypatch.setattr(ai_comment, 'AsyncOpenAI', factory)
    assert await ai_comment.generate_ai_comment('stats') == ''
    factory.assert_not_called()


async def test_ai_manual_switch_off_with_key(monkeypatch):
    monkeypatch.setattr(conf, 'majsoul_ai_api_key', 'dummy-ai-key')
    monkeypatch.setattr(conf, 'majsoul_ai_comment', False)
    factory = Mock()
    monkeypatch.setattr(ai_comment, 'AsyncOpenAI', factory)
    assert await ai_comment.generate_ai_comment('stats') == ''
    factory.assert_not_called()


@pytest.mark.parametrize('error_class', [None, AuthenticationError, PermissionDeniedError])
async def test_ai_uses_own_key_and_handles_auth_rejection(monkeypatch, error_class):
    monkeypatch.setattr(conf, 'majsoul_ai_api_key', ' Bearer dummy-ai-key ')
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-paifuya-key')
    monkeypatch.setattr(conf, 'majsoul_ai_model', 'test-model')
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='comment'))]))
    if error_class:
        response = httpx.Response(401 if error_class is AuthenticationError else 403,
                                  request=httpx.Request('POST', 'https://example.com'))
        create.side_effect = error_class('dummy-ai-key sensitive upstream error', response=response, body={})
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    factory = Mock(return_value=manager)
    monkeypatch.setattr(ai_comment, 'AsyncOpenAI', factory)
    if error_class:
        with pytest.raises(QueryError) as caught:
            await ai_comment.generate_ai_comment('stats')
        assert '认证' in str(caught.value) and 'dummy-ai-key' not in str(caught.value)
    else:
        assert await ai_comment.generate_ai_comment('stats') == 'comment'
    assert factory.call_args.kwargs['api_key'] == 'dummy-ai-key'
    create.assert_awaited_once()


@pytest.mark.parametrize('command', ['雀魂PT图', '雀魂PT推移图', '雀魂PT走向图',
                                    '雀魂PT走向查询', '雀魂三麻PT走向查询', '雀魂四麻PT查询'])
async def test_pt_commands_block_before_search(context, monkeypatch, command):
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', '  ')
    monkeypatch.setattr(conf, 'majsoul_ai_api_key', 'dummy-ai-key')
    search = AsyncMock(side_effect=AssertionError('No external request expected'))
    for client in plot.api.values():
        monkeypatch.setattr(client, 'search_player', search)
    plugin = Majsoul(context, {})
    result = await collect(plugin.query(Event('/' + command + ' example')))
    assert 'PT 走向查询和绘图已关闭' in result[0].chain[0].text
    search.assert_not_awaited()


async def test_pt_service_without_key_also_blocks(monkeypatch):
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'Bearer ')
    search = AsyncMock()
    monkeypatch.setattr(plot.api[PlayerNum.three], 'search_player', search)
    with pytest.raises(QueryError, match='已关闭'):
        await plot.handle_majsoul_pt_plot(AsyncMock(), 'example', PlayerNum.three)
    search.assert_not_awaited()


async def test_key_enables_pt_and_regular_info_still_works_without_keys(context, monkeypatch):
    import astrbot_plugin_mahjongsoul.paifuya.query_majsoul_info as info
    plugin = Majsoul(context, {})
    plugin.bindings = SimpleNamespace(data={})
    plugin.query_lock = asyncio.Lock()
    info_handler, pt_handler = AsyncMock(), AsyncMock()
    monkeypatch.setattr(info, 'handle_majsoul_info', info_handler)
    monkeypatch.setattr(plot, 'handle_majsoul_pt_plot', pt_handler)
    await collect(plugin.query(Event('/雀魂信息 example')))
    info_handler.assert_awaited_once()
    pt_handler.assert_not_awaited()
    plugin.records = AsyncMock()
    await collect(plugin.query(Event('/雀魂三麻对局 example')))
    plugin.records.assert_awaited_once()
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-paifuya-key')
    await collect(plugin.query(Event('/雀魂三麻PT走向查询 example')))
    pt_handler.assert_awaited_once()
    assert pt_handler.await_args.args[1:] == ('example', PlayerNum.three)


async def test_same_user_cooldown_blocks_other_client(monkeypatch):
    from test_auth import mock_api
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-paifuya-key')
    monkeypatch.setattr(api, '_REQUEST_INTERVAL', 0)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '86400'}, text='rate limited')
    first, second = await mock_api(handler), await mock_api(handler)
    try:
        for client in (first, second):
            with api.paifuya_user('test:101'), pytest.raises(PaifuyaRateLimitError):
                await client.search_player('example')
        assert len(calls) == 1
    finally:
        await first.close()
        await second.close()


async def test_status_reports_effective_gates_without_keys(context, monkeypatch):
    plugin = Majsoul(context, {})
    result = await collect(plugin.api_status(Event('/雀魂接口状态', admin=True)))
    text = result[0].chain[0].text
    assert 'PT 走向查询及绘图：已关闭' in text and 'AI 锐评：已关闭' in text
    monkeypatch.setattr(conf, 'majsoul_paifuya_api_key', 'dummy-paifuya-key')
    monkeypatch.setattr(conf, 'majsoul_ai_api_key', 'dummy-ai-key')
    result = await collect(plugin.api_status(Event('/雀魂接口状态', admin=True)))
    text = result[0].chain[0].text
    assert 'PT 走向查询及绘图：已开启' in text and 'AI 锐评：已开启' in text
    assert 'dummy-' not in text
