---
name: wash-queue-console
description: >-
  本地「洗片控制台（Wash Queue Console）」与 LibTV / 即梦官网 Seedance 2.5 后台协同自动化协议。
  只要用户需要操作洗片控制台 `server.py` 服务、`agent_bridge.py` 并发 Worker、前台接手调试镜头、
  或处理 `queue.json` 中的 queued / run_requested / prompt_update_requested / feedback_queued / final_requested / recheck_requested
  任务时，立即触发并遵循本 skill。
---

# 洗片控制台（Wash Queue Console）跨会话协同协议

本工作区的桌面应用 `洗片控制台.app` 与本地服务 `server.py`（端口 `8787`，源码位于 `<workspace>/tools/wash_queue_app/` 或应用包内 `Contents/Resources/app/`）完全独立运行；所有项目数据均持久化在当前工作区的 `wash_queue_data/` 目录（`queue.json`、`presets/` 与 `shots/`，具体路径也可从 `GET http://127.0.0.1:8787/api/state` 读取，且绝不上传或写入个人敏感路径）。

---

## 1. 桌面「一镜一对话」+ 纯本地 Fast-Path 与单镜接手

> [!IMPORTANT]
> 本系统默认采用 **「桌面一镜一对话（`desktop_subagent`）」+ 纯 Python Fast-Path 混合流水线**（也可在右上角状态弹窗切换回 `cli_bridge`）：
> 1. **纯机械操作走零 LLM Fast-Path（`agent_bridge.py`）**：改词同步（`prompt_update_requested`）、即梦 480P 样片生成/下载（`run_requested`）与 1080P 转正（`final_requested`）在默认模式下直接由后台 `agent_bridge.py` 走纯 Python Fast-Path 直出，**绝不触发外部 `agy -p` CLI 进程**、无任何登录弹窗；若纯机械尝试无法处理，自动通过 `/api/shots/{shot_id}/release-fastpath` 退回 AI 队列。
> 2. **AI 思考任务走桌面「一镜一对话」（Antigravity 主控 + Subagent 1~2 路并发）**：首次拆镜写词（`queued`）、返工修改（`feedback_queued`）、自检修复（`recheck_requested`）及 LibTV 或 Fast-Path 退回任务，由 Antigravity 桌面主控通过 `scripts/wait_next_ai_task.py` 维持 `"desktop"` 心跳并响应式唤醒：
>    - 每次调用 `POST http://127.0.0.1:8787/api/agent/coordinator-claim` 领取最多 **1~2** 个 AI 镜头；
>    - **一镜一专属子对话**：若 `desktopConversationId` 已有且 `canResumeDesktopSubagent` 为 true，则直接用 `send_message` 唤醒该镜头专属子对话；否则用 `invoke_subagent`（`TypeName="self"`）开启全新专属子对话，并立即调用 `POST /api/shots/{shot_id}/bind-conversation` 绑定 `conversationId`；
>    - 每个单镜子对话在完成后直接调用 `POST /api/shots/{shot_id}/agent-update` 回写 `desktopConversationId` 与成片/画布结果（**严禁**在任何单镜子对话内再递归挂起队列监听）。

1. **检查服务与 Fast-Path 执行器是否在线**（优先使用工作区 `tools/wash_queue_app`，否则自动匹配常见应用目录内的 `洗片控制台.app/Contents/Resources/app`；也可先读 `GET http://127.0.0.1:8787/api/agent/status` 的 `desktopCoordinatorGuide` 获取当前运行实例的精确 `pythonBin`/`waitScript`/`baseUrl`）：
   ```bash
   APP_DIR="$( [ -d ./tools/wash_queue_app ] && pwd )/tools/wash_queue_app"
   [ -d "$APP_DIR" ] || APP_DIR="/Applications/洗片控制台.app/Contents/Resources/app"
   [ -d "$APP_DIR" ] || APP_DIR="$HOME/Applications/洗片控制台.app/Contents/Resources/app"
   [ -d "$APP_DIR" ] || APP_DIR="$HOME/Desktop/洗片控制台.app/Contents/Resources/app"
   [ -d "$APP_DIR" ] || APP_DIR="$HOME/Desktop/洗片控制台-同事版/洗片控制台.app/Contents/Resources/app"
   PY_BIN="$APP_DIR/.venv/bin/python"
   [ -x "$PY_BIN" ] || PY_BIN="$APP_DIR/../runtime/bin/python3.12"
   [ -x "$PY_BIN" ] || PY_BIN="$HOME/.wash_console/venv_x64/bin/python"
   [ -x "$PY_BIN" ] || PY_BIN="python3"
   curl -sf http://127.0.0.1:8787/api/state >/dev/null || bash "$APP_DIR/launch.sh" --no-browser
   ```
2. **桌面主控挂起响应式监听**：
   - 将模式确认切为桌面主控：`POST http://127.0.0.1:8787/api/config/ai-dispatch-mode`（`{"mode":"desktop_subagent"}`）
   - 查看待办队列：`curl -sf http://127.0.0.1:8787/api/agent/pending`
   - 启动桌面心跳与待办阻塞等待（作为后台非守护命令 `WaitMsBeforeAsync=500`）：
     `"$PY_BIN" "$APP_DIR/scripts/wait_next_ai_task.py" --timeout 28800`
   - 桌面主控领单：`POST http://127.0.0.1:8787/api/agent/coordinator-claim`（`{"workerId":"desktop_coordinator","limit":2}`）
   - 绑定单镜子对话：`POST http://127.0.0.1:8787/api/shots/{shot_id}/bind-conversation`（`{"conversationId":"<subagent_id>","surface":"desktop"}`）

---

## 2. 必须联动的核心 Skills 与双平台路由（LibTV / 即梦官网）

每个镜头带有 `platform` 字段（`"libtv"` 或 `"jimeng"`）以及 `workMode` 字段（`"video_wash"` 洗视频默认 / `"video_creation"` 正常生成），请按平台与模式分流处理：

- **当 `workMode` 为 `"video_creation"`（正常生成模式）**：参考视频非必需（支持无图纯文生 `--mode t2v` 或仅构图/角色图 `--mode m2v`）。**仅加载并遵循 `seedance-video-prompts`**（若目标平台为 `libtv` 辅以 `libtv-cli`；若为 `jimeng` 且队列已由 Fast-Path 内联给出命令则无需重复读 `dreamina-canvas-cli` / `schema`），**严禁加载或执行** `seedance-video-recreation`、`libtv-seedance-batch` 及 720P 深度捕捉。
- **目标生成时长（`targetDuration`）**：支持 `video_creation`（正常生成）与 `video_wash`（洗视频）双模式；只要用户指定了 `4–30` 整数秒（或在修改意见/详情页调整了时长滑块），均须优先按该秒数编排节拍并严格传入 `--duration`。未指定（自动）时，`video_wash` 自动对齐原视频长度不变，`video_creation` 默认 `5s`。
- **当 `workMode` 为 `"video_wash"`（默认洗视频模式）**：
  - **当 `platform` 为 `"libtv"`（默认）**：加载并遵循 `libtv-cli`、`libtv-seedance-batch`、`seedance-video-recreation`、`seedance-video-prompts`；使用 `libtv` CLI 与 `{{Node nodeKey}}` 引用，保留网页「深度动作捕捉」流程与 `libtvMixedOrder`。
  - **当 `platform` 为 `"jimeng"`（即梦官网）**：加载并遵循 `dreamina-canvas-cli`（工作区 `.agents/skills/dreamina-canvas-cli/SKILL.md` 或全局 skill）、`seedance-video-recreation`、`seedance-video-prompts`；使用 `dreamina-canvas --non-interactive --format json` 显式传入 `--project-id <projectUuid>`，以 `{{node:<node-id>}}` 写正文引用并遵循 `dreamina-canvas-cli` 的积分报价/确认与 7 天样片有效期规则，仅将公开可恢复 ID（`nodeId` / `submitId` / `selectedResourceId`）写入 `jimengMeta`。

处理任何队列镜头前，**按任务性质同时加载并严格遵循**：
- `libtv-seedance-batch`：LibTV 画布节点、`深度动作捕捉`、Seedance 2.5（样片 480P / 正片 1080P、21:9、对齐原片整数秒）、最小必要人脸涂白（`$APP_DIR/scripts/whiteout_mask.py` 或工作区 `tools/wash_queue_app/scripts/whiteout_mask.py`）、不代点“生成”。
- `seedance-video-recreation`：多模态直读 `sourceVideo` 原片与音轨，还原镜头运动、走位方位、节拍与对白，并用简洁自然的素材绑定句声明 `@图片N` 与 `@视频N`。
- `seedance-video-prompts`：严格遵守公式结构、`这是一个一镜到底的连续长镜头，全程不切镜、不剪辑`、Dolly In 物理措辞，**影调由对应 `@图片N` 承载时不得在开头堆砌风格或重复服装细节**，结尾固定附 Arri Flex435 摄影机预设。

> [!CAUTION]
> 提示词中 **绝不允许出现** “涂白”“白底”“遮罩”“whiteout”等字样；提示词须结构精炼、动作与运镜精确，完全贴合 `seedance-video-prompts` 与 `seedance-video-recreation`。

---

## 3. 四类待办状态的处理规范

### A. `queued`（新建排片）
1. 读取 `shot` 全量元信息（`GET http://127.0.0.1:8787/api/shots/{id}` 或 Worker 简报）：原视频 `sourceVideo`、有序图片列表 `compImage` + `charRefs`（序号即 `图片1..N`）、`whiteoutOptions`（`parts` / `targetSlot`）、`userPrompt`、`scene`。
2. 如需涂白（默认先检查 `whiteoutOptions.parts`，未勾选时仅做“最小必要人脸防阻塞涂白”；若已有手绘涂白稿 `filePath` 则直接使用）：用 `whiteout_mask.py` 或审慎遮罩生成洁白人脸/指定部位图，注意领口、围巾、肩部光影自然留存。
3. 多模态直读原片（`read_file` 读取 `.mp4`）与参考图，撰写严格符合 `seedance-video-prompts` + `seedance-video-recreation` 的提示词并保存至 `outputs/{shot_id}_提示词.md`。
4. 上传并连线到 LibTV 画布（遵循 `libtv-cli` 与 `libtv-seedance-batch`），设置 `mixedList`、`aspectRatio=21:9`、对齐原片整数秒、480P 样片模式，**不点击生成**；用 Chrome/截图核验画布节点连线与缩略图无误。
5. 调用 `POST http://127.0.0.1:8787/api/shots/{shot_id}/agent-update` 回填状态 `ready_on_canvas`、`promptText`、`libtv`（或 `jimengMeta`）、`agentSummary`、`revisionDirection`（简述首版思路与核心保留项，写入版本历史供回滚对照）、`draftPath`（若已出片）；**若本镜生成了局部涂白图（如 `comp_masked.png`），务必同时传入 `whiteoutUpdates: [{"slotTag": "图片1", "filePath": "...", "parts": [...]}]`（即梦另附 `jimengMeta.maskedCompPath`），确保控制台缩略图与画板即时展示涂白效果并保留原图对比。**

### B. `feedback_queued`（修改意见 / @新图N / 改词）
1. 解析 `latestFeedback` 中的文字修改意见、标签、`editedPrompt`、`targetSlot` 及新增附件 `newAttachments`（对应提示词中的 `@新图N`）。
2. 如涉图片调整（重新涂白、更换参考槽位或接入 `@新图N`），先上传/替换素材并把提示词中的 `@新图N` 绑定为正式的 `@图片K` 槽位号，重新校验提示词结构与 LibTV `mixedList` 顺序。
3. 更新 LibTV 节点后回传 `POST /api/shots/{shot_id}/agent-update`（并将反馈标记为 `resolveFeedbacks: true`）。**务必同时提供 `revisionDirection`**（1–2 句话写清你理解的修改方向：改了什么、保留了什么、原因是什么），系统会自动展示在提示词区并记入本轮版本历史，支持随时按对话轮次回滚。

### C. `recheck_requested`（用户点击「↻ 重新处理·自检」）
用户因处理中断、效果疑虑或想核对一致性触发该状态时，**必须执行完整断点续传 + 全链路自检**：
1. 对比 `queue.json` 中本镜资产、`outputs/{shot_id}_提示词.md` 与 LibTV 画布现存节点（`libtv node <nodeKey>` + Chrome 截图检查）：
   - 缺失深度视频 / 图像节点 / 边连线断连时，补传补连；
   - 已选中 `whiteoutOptions` 却未生成或未同步 `mask` 节点时，补齐涂白并同步到对应槽位；
   - 按 `seedance-video-prompts` 与 `seedance-video-recreation` 重审并修复提示词（检查是否误把摄影机预设写到开头、是否啰嗦重复、是否错写人物左右站位、是否漏用 `@图片N`/`@视频N` 绑定、是否带入禁词）；
   - 核对 `compImage` 与 `charRefs` 序号和提示词中 `@图片N`、LibTV `mixedList` 是否严格一一对应。
2. 全部校验修复完成后，更新 `POST /api/shots/{shot_id}/agent-update`，把 `status` 恢复为合适状态（如 `ready_on_canvas` 或 `draft_ready`）并在 `agentSummary` 与 `revisions` 写明逐项核查结果。

### D. `final_requested`（转为 1080P 正片并下载归档）
按 `libtv-seedance-batch` 执行 `libtv video final -n <sampleNodeKey> -p <projectUuid> [-g <groupKey>]` 转出 1080P 正片并下载至 `shots/{shot_id}/final_1080p.mp4`，回写 `status: "final_ready"`、`finalVideo: { filePath, remoteUrl, watermark }` 与本地正片路径；即梦官网执行 `dreamina-canvas node promote video-sample` 时，若 `brief.jimengCreditCeiling > 0` 且报价在该上限内，直接传 `--credit-ceiling <jimengCreditCeiling> --yes` 自动放行，超限或无法报价再停在 `needs_confirmation`。

### E. `run_requested`（用户在控制台点击「▶ 帮我 Run 生成」）
当画布节点已处于 `ready_on_canvas`（已就绪）或用户改完提示词后在 App 里点击「▶ 帮我 Run 生成」：
1. 若 `pendingPromptEdit` 非空，先将最新提示词同步至 `libtvSampleNodeKey`（`libtv node <sampleNodeKey> -p <projectUuid> [-g <groupKey>] --prompt "..."`）；若尚未建完节点先按 A 流程补齐。
2. **执行生成（遵守 `libtv-cli` 与即梦积分上限规范）**：LibTV 运行 `libtv node <sampleNodeKey> -p <projectUuid> [-g <groupKey>] --run` 同步阻塞等待终态 JSON（禁止自行轮询、禁止提前 kill、禁止后台化）；即梦官网先跑只读 `node quote` 核对报价，若 `brief.jimengCreditCeiling > 0` 且明确报价 `<= jimengCreditCeiling`，**禁止停在 `needs_confirmation`**，直接用 `node confirm --credit-ceiling <ceiling>` + `node run --credit-token <token>`（不写 token 入状态）发起生成；多路镜头并发时各自独立执行互不阻塞，且**无需占锁 Chrome UI**。
3. **下载做好的成片给用户看片**：`--run` 返回 `status == 2` 成功后，立刻调用 `POST http://127.0.0.1:8787/api/shots/{shot_id}/sync-libtv`（或 `libtv download -n <sampleNodeKey> -p <projectUuid> -o <dataRoot>/shots/{shot_id}/ --watermark false`，非会员降级 `--watermark true` 存入 `draft_480p.mp4`）。
4. **看片自检并回写结果**：多模态直读下载回来的 `draft_480p.mp4` 核对画面/动作/音画后，调用 `POST /api/shots/{shot_id}/agent-update`（带 `claimToken`、`status: "draft_ready"`、`draftVideo: { filePath, remoteUrl, watermark }`、`resolveFeedbacks: true` 若由反馈触发、`agentSummary` 附上简明自检结论），控制台右侧成片预览与反馈区即会立刻出现可播放视频供用户观看。

### F. `prompt_update_requested`（用户手动改词后点击「更新提示词」）
当用户在控制台提示词栏手动修改了提示词并点击「更新提示词」按钮时：
1. 读取最新 `promptText` 与 `previousPromptText` 了解用户改动的具体内容（同步知悉剧情/台词/镜头调度变更，写入对应上下文或修订记录）；若 `@图片N` / `@视频N` 有变动，一并核对 `libtvMixedOrder`。
2. 若该镜头已在 LibTV 画布建有节点，调用 `libtv node <sampleNodeKey> -p <projectUuid> [-g <groupKey>] --prompt "..."` 将最新提示词同步至画布节点（**无需代点 `--run` 生成**，等待用户随后决定何时点 Run）。
3. 调用 `POST /api/shots/{shot_id}/agent-update`（带 `claimToken`、恢复为 `promptUpdatePrevStatus` 或合理状态：已有 `draftVideo` → `draft_ready`，已上画布无成片 → `ready_on_canvas`、清除 `pendingPromptEdit`、在 `agentSummary`、`revisionSummary` 与 `revisionDirection` 写明已收到用户手动改动并同步至画布，归纳本次改词核心方向）。

---

## 4. 常用本地 API 速查（`http://127.0.0.1:8787`）

- 查看全局队列、待办与连接状态：`GET /api/state`、`GET /api/agent/pending`、`GET /api/agent/status`、`GET /api/shots/{shot_id}`
- 心跳与本地 `agy` 桥接控制：`POST /api/agent/heartbeat`、`POST /api/agent/bridge/start`、`POST /api/agent/bridge/stop`
- 批量/单镜原子签收为 `processing`：`POST /api/agent/claim`、`POST /api/shots/{shot_id}/claim`
- 并发互斥锁（TTL 自动过期防死锁）：`POST /api/agent/locks/{lock_name}/acquire`、`POST /api/agent/locks/{lock_name}/release`、`GET /api/agent/locks`
- 画布切换与新建：`GET /api/projects`、`POST /api/projects`、`PATCH /api/shots/{shot_id}/project`、`POST /api/config/default-project`
- 更新提示词 / 回滚历史版本 / 触发 Run / 拉取成片 / 转 1080P：`POST /api/shots/{shot_id}/update-prompt`、`POST /api/shots/{shot_id}/rollback`、`POST /api/shots/{shot_id}/run`、`POST /api/shots/{shot_id}/sync-libtv`、`POST /api/shots/{shot_id}/approve-final`
- 场次管理：`POST /api/scenes`、`POST /api/scenes/rename`、`DELETE /api/scenes?name=...`、`PATCH /api/shots/{shot_id}/scene`
- Agent 回写结果与进度（并发必须带 `claimToken`）：`POST /api/shots/{shot_id}/agent-update`
- 触发中断重检：`POST /api/shots/{shot_id}/reprocess`
- 前台 Terminal / Antigravity 接手单镜：`POST /api/shots/{shot_id}/open-chat`、`POST /api/shots/{shot_id}/handoff-context`、`POST /api/system/open-antigravity`

---

## 5. 多任务并发调度协议（独立桥接池 4 路并发）

本系统通过后台 `agent_bridge.py` 实现 **多路 `agy -p` 独立会话真并行执行**（每镜独享独立上下文与 `--conversation` 记忆）：

1. **自动领单与并发执行**
   - 桥接池自动调用 `POST /api/agent/claim`（`workerId: "agy_bridge"`）按空闲槽位签收可运行镜头，并为每镜启动独立进程执行；纯改词同步优先直走 `shot_fastpath.py` Python 快速通道。
   - **禁止**在任何单镜 Worker 或前台接管对话内再挂起队列常驻监听。
2. **禁止全局状态互相踩踏（LibTV CLI 隔离规则）**
   - `.libtv/project.json` 是单文件共享状态，**并发场景下严禁调用 `libtv group use`**；
   - 所有 `libtv` 命令（`group list` / `group create` / `upload` / `node` / `seedance` / `edge` 等）**必须显式传入 `-p <projectUuid>`（以及 `-g <groupKey>` 或明确 `nodeKey`）**。
3. **重活真并行，仅对“共享 UI / 画布布局”短时加锁（~1–5 秒）**
   - **真并发执行**：`read_file` 多模态读取原片/参考图、生成 `whiteout_mask.py` 涂白图、撰写 Seedance 提示词与 `outputs/{shot_id}_提示词.md`、上传素材到 LibTV、建文本/Seedance 节点、连线、校验与下载，所有 Worker 全程互不阻塞并行执行。
   - **布局短锁（~1 秒）**：在同一张画布新建分组前，先申请画布级布局锁，查清最大右边界并创建不重叠新分组后**立即释放**：
     `POST /api/agent/locks/canvas_layout_<projectUuid>/acquire`（`{"owner": "<shotId>", "ttlSec": 45, "waitSec": 30}`）-> 建组占位 -> `release`。
   - **Chrome UI 短锁（~3–5 秒，优先确定性 CDP 脚本）**：无论是 LibTV 还是即梦官网模式，只要操作浏览器执行「选中视频节点 -> 点击 `深度动作捕捉` -> 点击 `720P` -> 点击 `生成`」这一瞬间，必须先申请全局 `chrome_ui` 锁：
     `POST /api/agent/locks/chrome_ui/acquire`（`{"owner": "<shotId>", "ttlSec": 60, "waitSec": 45}`）。**即梦官网模式**已由 `shot_fastpath.py` 在唤醒 Worker 前并发预上传构图/角色/原片节点并自动调用 `scripts/dreamina_depth_cdp.py` 通过 Chrome `DevToolsActivePort` 确定性点完 720P 深度动作捕捉（写入 `prepped.pretriggeredDepthNodeId`）；**当 `pretriggeredDepthNodeId` 已存在时，Worker 严禁再打开/操作 Chrome 点击深度捕捉**，直接 `node show` 等该节点就绪即可；仅当 `pretriggeredDepthNodeId` 为空时才先跑 `scripts/dreamina_depth_cdp.py` 补触发，仍失败再降级持 `chrome_ui` 锁用 Chrome MCP 点击。点完触发见到云端任务开始跑即可立即 `release` 释放锁（不必占用锁等云端转码完成），下一路 Worker 立刻接力点提取，云端 3–4 路深度视频生成同时并发推进！
   - **即梦官网 Fast-Path `queued` 极速路径**：当简报中 `prepped.prebuildReady == true` 时，严禁再用 `node find` 全表扫描画布或重复 `resource upload`，直接使用 `prepped.preuploadedCompNodeId`、`prepped.preuploadedCharRefNodes`、`prepped.preuploadedSourceNodeId`、`prepped.suggestedRowY` 与 `prepped.pretriggeredDepthNodeId`；视觉校验 `compImage` 仅在勾选 `whiteoutOptions.parts` 或画面含未遮罩真人正面人脸时才运行 `scripts/whiteout_mask.py` 上传 `{shot_id}-构图遮罩图`，否则直接复用预建构图节点，写出提示词后直接创建 `{shot_id}-样片`（`--x 3620 --y <suggestedRowY> --ref node:...`，禁止 `--run`）并只读 `node quote` 报价回写 `ready_on_canvas`。
   - **即梦官网 Fast-Path `run_requested` / `final_requested` 全直出通道**：若启用 `fastPathEnabled` 且设定了 `jimengCreditCeiling`，后台 `shot_fastpath.py` 直接在 Python 内完成 `node quote -> node confirm --credit-ceiling -> node run`（或 `node promote video-sample --credit-ceiling --yes`）、`operation wait` 分段等待、`resource download` 落盘与 1080P 自动重命名导出；`autoQaAfterRender` 关闭（默认极速）时零 `agy` 消耗直接回写 `draft_ready`/`final_ready`，开启时仅由 `hydrogen-low` 单步看片核验。
   - 若收到多次连续修改意见（队列简报中包含 `pendingFeedbacks[]`），必须合并核对并逐条落实全部未解决反馈后再传 `resolveFeedbacks: true`。
4. **乐观锁回写（`claimToken` 防冲突）**
   - 每个 Worker 在执行过程中可用自己的 `claimToken` 频繁回写 `status: "processing"` 实时刷新控制台中的 `agentSummary` 进度条；
   - 最终回写 `ready_on_canvas` / `needs_confirmation` / `draft_ready` / `final_ready` 时**必须携带 `claimToken`**；若服务返回 HTTP `409 Conflict`（说明用户在你处理期间提交了新反馈或点击了“重新处理·自检”，清除了旧令牌），立即停止旧流程，绝不覆盖用户新指令。
