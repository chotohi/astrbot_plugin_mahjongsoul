from pydantic import BaseModel, Field

DEFAULT_AI_PROMPT = """
你是一个雀魂猫娘。
你需要根据玩家的雀魂统计数据进行锐评。

要求：
- 说话要像猫娘一样
- 客观分析数据
- 有节目效果
- 可以玩梗
- 不要太长
- 80字以内
- 不要回复很多换行符
- 禁止 Markdown 和富文本
- 禁止使用 **加粗文本**
- 不要辱骂群友
- 重点分析打法问题
- 要给出问题的解决方案
""".strip()

def normalize_api_key(value: str) -> str:
    value = value.strip()
    if value.lower() == 'bearer':
        return ''
    if value.lower().startswith('bearer '):
        value = value[7:].strip()
    return value

class Config(BaseModel):

    majsoul_query_timeout: float = 15.0

    majsoul_font: str = ""
    majsoul_font_path: str = ""

    majsoul_send_aggregated_message: bool = True

    majsoul_send_link: bool = False

    # Paifuya player_records API key. Loaded from
    # MAJSOUL_PAIFUYA_API_KEY in the AstrBot settings file.
    majsoul_paifuya_api_key: str = ""
    majsoul_paifuya_cooldown_seconds: int = Field(default=3600, ge=1)
    majsoul_pt_max_games: int = Field(default=2000, ge=1)

    # AI锐评

    majsoul_ai_comment: bool = True
    majsoul_ai_prompt: str = DEFAULT_AI_PROMPT

    majsoul_ai_api_base: str = ""

    majsoul_ai_api_key: str = ""

    majsoul_ai_model: str = ""

    majsoul_ai_timeout: int = 60

    @property
    def ai_comment_enabled(self) -> bool:
        return self.majsoul_ai_comment and bool(normalize_api_key(self.majsoul_ai_api_key))

    @property
    def pt_query_enabled(self) -> bool:
        return bool(normalize_api_key(self.majsoul_paifuya_api_key))
    

    class Config:
        extra = "ignore"


conf = Config()
