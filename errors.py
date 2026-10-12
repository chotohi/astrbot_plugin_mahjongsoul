PT_KEY_REQUIRED = '未配置牌谱屋 API Key，PT 走向查询和绘图已关闭。请管理员填写 majsoul_paifuya_api_key 并重载插件。'


class PaifuyaRateLimitError(Exception):
    def __init__(self, *, retry_after=3600, auth_required=False, auth_sent=False):
        self.retry_after = retry_after
        self.auth_required = auth_required
        self.auth_sent = auth_sent
        message = '牌谱屋要求有效授权' if auth_required else '牌谱屋限制了请求频率'
        message += '；本次已携带牌谱屋密钥，但服务端未放行' if auth_sent else '；本次未携带牌谱屋密钥，请检查 majsoul_paifuya_api_key'
        message += f'。已停止自动重试，当前用户还需等待 {retry_after:g} 秒（其他用户独立计时）；若为授权问题，需核实密钥是否有效。'
        super().__init__(message)

class BadRequestError(ValueError):
    pass

class QueryError(RuntimeError):
    pass
