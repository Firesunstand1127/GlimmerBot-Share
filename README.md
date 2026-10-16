# MaaShanShuo 分享版

Windows / MuMu Android 12 的《闪烁之光》桌面自动化助手，基于 MaaFramework。当前版本 **v0.6.15 候选版**。

从 Releases 下载 Windows ZIP，完整解压后运行 `MaaShanShuoShare.exe`。首次使用会打开本地设置，填写自己的设备、已保存的 SDK 账号显示名、角色名和区服。程序不需要账号密码。详细操作见 [使用说明](ui/USER_GUIDE.txt)。

支持日常任务、主城提醒、位面征战及独立的星河神殿守候。星河目标由本地设置指定，检查间隔可直接输入 1～86400 秒，并在运行中调整。免费次数和已有竞技场券耗尽时暂停，不购买补充。不同日常可能使用游戏内金币或已有材料，运行前请检查功能说明和勾选项。

本版增加按账号、功能和业务结果清理截图/日志：保留最新成功与最新无需操作记录；失败/未知等诊断最多三份、三天；正在运行和战果待确认的记录受保护。容量目标为 1 GiB，保护记录可能导致暂时超限。可在总览查看占用并手动清理。

本仓库是独立的干净源码快照，不包含旧仓库历史、私人账号默认值、运行截图、日志、消费账本和本地配置。设置与运行状态只写入本机 `%LOCALAPPDATA%\MaaShanShuoShare`。反馈问题前请检查截图、OCR结果和日志中的个人信息。

已完成离线测试、资源检查、界面检查及打包启动验证。分享版尚未在朋友设备上完成完整游戏流程验收；需要 MuMu Android 12、1080×1920 竖屏及对应游戏渠道。识别按720×1280归一化，其他分辨率或游戏界面可能不兼容。

## 源码运行与构建

源码入口为 `ui/app.py`，Python依赖见 `requirements-runtime.txt`。当前运行时使用 `maafw==5.13.0b2`，该预发行版本可能无法从公开 PyPI 获取；普通用户推荐使用 Release 中自带运行时的 ZIP。

已具备对应运行时和构建依赖时，在 PowerShell 中执行 `ui/build.ps1`。可通过 `MAASHANSHUO_PYTHON`、`MAASHANSHUO_RUNTIME`、`MAASHANSHUO_PACKAGER` 指定 Python、依赖及 PyInstaller 目录。构建只复制白名单文件，不复制本地日志或配置。

离线检查：`python -m unittest discover -s tools -p '*_test.py'`；资源引用检查：`python tools/pipeline_audit.py`；消费规则检查：`python tools/pipeline_policy_test.py`。Schema 检查需另提供 MaaFramework 官方 schema 目录。

项目代码采用 MIT 许可，见 [LICENSE](LICENSE)。MaaFramework、MaaAgentBinary、PySide6、OCR模型等第三方组件遵循各自许可；分发包保留其可用的许可文件。游戏素材仅用于识别。
