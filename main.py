from astrbot.api.star import Star
import asyncio
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from ._common import NativePlugin, JsonStore, data_dir, arguments


class Majsoul(NativePlugin, Star):
    async def initialize(self):
        from .config import Config, conf
        conf.__dict__.update(Config(**self.options).__dict__)
        bindings_dir = data_dir('astrbot_plugin_mahjongsoul')
        bindings_path = bindings_dir / 'bindings.json'
        self.bindings = JsonStore(bindings_path)
        # Preserve existing bindings when upgrading from the old data directory.
        legacy_path = bindings_dir.parent / 'astrbot_plugin_majsoul' / 'bindings.json'
        if not bindings_path.exists() and legacy_path.is_file():
            self.bindings.data = JsonStore(legacy_path).data
            self.bindings.save()
        self.query_lock = asyncio.Lock()
        from .paifuya.data.api import paifuya_api, prober
        self.api = paifuya_api
        self.prober = prober
        for api in self.api.values():
            if api.client.is_closed:
                api.__init__(api._baseurl, api.player_num)
        await self.prober.start()
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import font_manager, rcParams
        font = Path(conf.majsoul_font_path) if conf.majsoul_font_path else Path(__file__).parent / 'paifuya/NotoSansCJK-Regular.otf'
        if font.is_file():
            font_manager.fontManager.addfont(str(font))
            rcParams['font.family'] = font_manager.FontProperties(fname=str(font)).get_name()

    def user_key(self, event):
        return f'{event.get_platform_id()}:{event.get_sender_id()}'

    @filter.command('雀魂接口状态')
    async def api_status(self, event: AstrMessageEvent):
        event.stop_event()
        if not event.is_admin():
            yield event.plain_result('此命令需要 AstrBot 管理员权限。')
            return
        from .config import conf
        from .paifuya.data.api import normalize_api_key, prober
        loaded = bool(normalize_api_key(conf.majsoul_paifuya_api_key))
        yield event.plain_result(
            '雀魂插件 v1.0.2\n牌谱屋密钥 majsoul_paifuya_api_key：' + ('已加载' if loaded else '未加载')
            + '\nPT 走向查询及绘图：' + ('已开启' if conf.pt_query_enabled else '已关闭（未配置牌谱屋 Key）')
            + f'\nPT 图场次上限：{conf.majsoul_pt_max_games}'
            + f'\n牌谱屋错误等待：{conf.majsoul_paifuya_cooldown_seconds} 秒（按 QQ 用户独立计时）'
            + '\nAI 锐评：' + ('已开启' if conf.ai_comment_enabled else '已关闭（未配置 AI Key 或手动关闭）')
            + '\n当前牌谱屋镜像：' + prober.host
            + '\n牌谱屋请求使用 Authorization: Bearer，AI 锐评密钥不用于牌谱屋。'
            + '\n请求节流：最小间隔 1.10 秒（低于最大 1 QPS）。'
            + '\n此检查不发请求，不代表密钥已被服务端接受。'
        )

    @filter.command('雀魂账号绑定')
    async def bind(self, event: AstrMessageEvent):
        event.stop_event()
        name = arguments(event)
        if name:
            if len(name) > 15:
                yield event.plain_result('昵称不能超过 15 字')
                return
            self.bindings.data[self.user_key(event)] = name
            self.bindings.save()
        yield event.plain_result('当前绑定：' + self.bindings.data.get(self.user_key(event), '未绑定'))

    @filter.command('雀魂账号解绑')
    async def unbind(self, event: AstrMessageEvent):
        event.stop_event()
        self.bindings.data.pop(self.user_key(event), None)
        self.bindings.save()
        yield event.plain_result('已解除绑定')

    @filter.regex(r'^/?雀魂(?:(?:三|四)麻)?(?:信息|查询|对局|[Pp][Tt](?:推移|走向)?(?:图|查询))(?:\s|$)', priority=20)
    async def query(self, event: AstrMessageEvent):
        from .paifuya.data.models.player_num import PlayerNum
        from .paifuya.parsers.room_rank import try_parse_room_rank
        from .paifuya.parsers.time_span import try_parse_time_span
        from .paifuya.parsers.limit_of_games import try_parse_limit_of_games
        from .paifuya.query_majsoul_info import handle_majsoul_info
        from .paifuya.query_majsoul_pt_plot import handle_majsoul_pt_plot
        from .paifuya.data.api import paifuya_user
        event.stop_event()
        from .config import conf
        from .errors import PT_KEY_REQUIRED
        if 'pt' in event.message_str.split()[0].lower() and not conf.pt_query_enabled:
            yield event.plain_result(PT_KEY_REQUIRED)
            return
        num = PlayerNum.three if '三麻' in event.message_str.split()[0] else PlayerNum.four
        kwargs, names = {}, []
        try:
            for arg in arguments(event).split():
                if rooms := try_parse_room_rank(arg):
                    kwargs['room_rank'] = rooms[0 if num == PlayerNum.four else 1]
                elif span := try_parse_time_span(arg):
                    kwargs['start_time'], kwargs['end_time'] = span
                elif limit := try_parse_limit_of_games(arg):
                    kwargs['limit'] = limit
                else:
                    names.append(arg[3:] if arg.lower().startswith('id:') else arg)
            name = ' '.join(names) or self.bindings.data.get(self.user_key(event))
            if not name:
                yield event.plain_result('请指定雀魂昵称，或先使用「雀魂账号绑定 昵称」')
                return
            async def emit(chain):
                await event.send(event.chain_result(chain))
            with paifuya_user(self.user_key(event)):
                async with self.query_lock:
                    async with asyncio.timeout(float(self.options.get('majsoul_query_timeout', 60)) or None):
                        if '对局' in event.message_str.split()[0]:
                            await self.records(event, name, num, kwargs)
                        elif 'pt' in event.message_str.split()[0].lower():
                            await handle_majsoul_pt_plot(emit, name, num, **kwargs)
                        else:
                            await handle_majsoul_info(emit, name, num, **kwargs)
        except TimeoutError:
            yield event.plain_result('雀魂查询超时，请稍后重试')
        except Exception as exc:
            yield event.plain_result(f'雀魂查询失败：{exc}')

    async def records(self, event, name, num, options):
        from .paifuya.data.models.room_rank import all_four_player_room_rank, all_three_player_room_rank
        from .paifuya.data.models.player_num import PlayerNum
        from .paifuya.mappers.game_record import map_game_record
        api = self.api[num]
        players = await api.search_player(name)
        if not players:
            await event.send(event.plain_result('未找到金之间以上的对局数据'))
            return
        rooms = options.get('room_rank') or (all_four_player_room_rank if num == PlayerNum.four else all_three_player_room_rank)
        for player in players:
            records = await api.player_records(player.id, options.get('start_time', datetime(2010, 1, 1, tzinfo=timezone.utc)),
                                               options.get('end_time', datetime.now(timezone.utc)), rooms,
                                               limit=min(options.get('limit', 10), 50), descending=True,
                                               api_key=self.options.get('majsoul_paifuya_api_key') or None)
            stream = StringIO()
            stream.write(f'昵称：{player.nickname}\n')
            for record in records:
                map_game_record(stream, record, player.id)
                stream.write('\n')
            await event.send(event.plain_result(stream.getvalue()))

    async def terminate(self):
        await super().terminate()
        if hasattr(self, 'prober'):
            await self.prober.close()
        if hasattr(self, 'api'):
            await asyncio.gather(*(api.close() for api in self.api.values()))
