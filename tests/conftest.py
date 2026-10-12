import os
import atexit
import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

# AstrBot creates default runtime data on import; keep it outside the repository.
_original_cwd = Path.cwd()
_runtime = tempfile.TemporaryDirectory(prefix='majsoul-tests-', ignore_cleanup_errors=True)
os.chdir(_runtime.name)

def _cleanup_runtime():
    os.chdir(_original_cwd)
    _runtime.cleanup()

atexit.register(_cleanup_runtime)

from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType, PlatformMetadata
from astrbot.api.message_components import Plain
from astrbot.api.star import StarTools


class Event(AstrMessageEvent):
    def __init__(self, text='', *, user='101', group='201', platform='test', self_id='999', admin=False, raw=None, chain=None):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        msg.self_id, msg.message_id, msg.group_id = self_id, '1234', group
        msg.sender = MessageMember(user_id=user, nickname='Tester')
        msg.message, msg.message_str = chain if chain is not None else [Plain(text)], text
        msg.raw_message = raw or {}
        super().__init__(text, msg, PlatformMetadata('aiocqhttp', 'test adapter', platform), group or user)
        self.role = 'admin' if admin else 'member'
        self.sent = []
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={'message_id': 6789}))

    async def send(self, result):
        self.sent.append(result)
        self._has_send_oper = True


async def collect(generator):
    return [value async for value in generator]


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def directory(name=None):
        path = tmp_path / (name or 'unknown')
        path.mkdir(parents=True, exist_ok=True)
        return path
    monkeypatch.setattr(StarTools, 'get_data_dir', staticmethod(directory))


@pytest.fixture
def context():
    return SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[]))


@pytest.fixture(autouse=True)
def reset_api_state(monkeypatch):
    import asyncio
    from astrbot_plugin_mahjongsoul.config import Config, conf
    from astrbot_plugin_mahjongsoul.paifuya.data import api
    for key, value in Config().model_dump().items():
        monkeypatch.setattr(conf, key, value)
    monkeypatch.setattr(api, '_cooldowns_by_user', {})
    monkeypatch.setattr(api, '_request_semaphore', asyncio.Semaphore(1))
    monkeypatch.setattr(api, '_next_request_at', 0.0)
