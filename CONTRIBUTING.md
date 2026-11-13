# 参与开发

使用 Python 3.12 或更高版本。插件需要在 AstrBot 内运行，不能直接执行 main.py。

```bash
python -m venv .venv
# Linux/macOS：source .venv/bin/activate
# Windows PowerShell：.venv/Scripts/Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
python -m pytest tests_card -q
python tools/package.py
```

`tests/` 需要 AstrBot 运行时；`tests_card/` 只依赖 Pillow，用于验证战绩卡的统计与版式，没装 AstrBot 时也能单独跑。

开发依赖固定 AstrBot 4.28.2，用于复现本次离线验证。测试使用模拟 HTTP、临时目录及虚构凭证，不需要雀魂账号、牌谱屋密钥或真实 QQ 连接。

提交功能变更时，请描述触发命令、预期行为、实际行为及验证方式。涉及 HTTP 的改动需保留限流与授权拒绝处理，不要通过重试或切换镜像绕过服务端冷却。

提交问题时请附 Python、AstrBot、插件版本以及脱敏日志。不要上传配置原文件、Cookie、密码、API 密钥、运行数据库或真实用户绑定表。

代码沿用本项目 AGPL-3.0 许可证，修改上游代码时保留来源说明；第三方资源需附对应许可。发布步骤见 [docs/RELEASING.md](docs/RELEASING.md)。
