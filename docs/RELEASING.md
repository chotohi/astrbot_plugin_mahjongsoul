# 发布说明

仓库：[chotohi/astrbot_plugin_mahjongsoul](https://github.com/chotohi/astrbot_plugin_mahjongsoul)。维护者：**ChoToHi**。

## 仓库简介

从 nonebot_plugin_majsoul 移植的 AstrBot 雀魂插件，支持玩家数据查询、最近对局和 PT 图，并新增可选 AI 锐评、接口诊断与限流处理。

建议主题：`astrbot`、`astrbot-plugin`、`majsoul`、`mahjong`、`python`。

## 发布流程

1. 按 CONTRIBUTING.md 安装开发依赖并运行测试。
2. 运行 `python tools/package.py`，检查 dist 下安装包与 SHA256SUMS.txt。
3. 提交代码后推送至本仓库，保留 LICENSE、NOTICE、UPSTREAM.md 和 licenses/。
4. 如需分发版本附件，在 GitHub 创建对应版本的 Release，并上传生成的安装包。

当前版本为 **v1.0.2**。版本变化时保持 metadata.yaml、main.py 中接口状态命令显示的版本号与安装包名称一致。版本变更及与 nonebot_plugin_majsoul 的功能差异见 CHANGELOG.md。

源码提交不代表已经创建 Release 或上架 AstrBot 插件市场；二者可另行办理。

不要提交个人配置、Key、Cookie、用户绑定、日志或数据库。仓库根目录下的运行数据目录 `/data/` 被忽略；业务源码 `paifuya/data/` 必须保留。打包脚本使用显式文件清单，不会将 Git 信息或运行缓存包含在安装包中。
