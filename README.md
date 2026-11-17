# 洗片控制台（Wash Queue Console）· GitHub 热更新源

本仓库托管 **洗片控制台（Seedance 2.5 双平台工作台 · LibTV / 即梦官网）** 的纯净应用源码与通用工作流 Skills，供桌面端 `洗片控制台.app` 进行零配置一键热更新。

- **当前最新版本**：`v2026.11.17.12`
- **更新源仓库**：`https://github.com/hqy-maker/wash-queue-console`

## 同事如何安装与热更新

1. **首次安装**：直接解压使用发放的「洗片控制台（同事版）」安装包，双击运行目录内的 `首次使用请先双击我（配置环境并启动）.command` 完成免配置初始化（内置 Python 运行时、`libtv` 与 `dreamina-canvas` CLI）。
2. **日常热更新**：打开控制台后，右上角版本胶囊会自动检测本仓库的 `version.json`。当有新版本时，点亮提示 **`有新版 v...`**，点击即可查看更新说明并一键 **「立即热更新并重启」**——更新过程自动备份上一版本（支持随时回滚），**完全保留本地片源、历史版本、自定义角色库及平台登录状态**。

## 最新更新日志（v2026.11.17.12）

修复 AgentPresenceTracker 与 QueueStore 之间的 ABBA 互锁死锁（拆分独立 _token_lock、在锁外执行 record_heartbeat/snapshot 与 notify_waiters，并将 /api/health 设为 async），彻底解决并发长轮询/活动心跳触发全部工作线程卡死导致视频播放无限转圈与界面无响应问题

## 目录结构

- `version.json`：远端版本清单（由控制台自动检查比对）
- `app/`：前后端控制台源码（FastAPI + Apple 极简前端 + `updater.py` 热更新引擎）
- `skills/`：7 项通用视频清洗与工作流 Skills
