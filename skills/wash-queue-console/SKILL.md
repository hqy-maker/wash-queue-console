---
name: wash-queue-console
description: >-
  本地「洗片控制台（Wash Queue Console）」与 LibTV / 即梦官网 Seedance 2.5 后台协同自动化协议。
  只要用户在新对话中说“启动洗片监听”“连上洗片控制台”“看下洗片控制台”“处理控制台排片队列”
  “有人在控制台点重新处理·自检”，或需要操作洗片控制台 `server.py` 服务、
  `watch_queue.py` 队列监听器、`queue.json` 中的 queued / run_requested / prompt_update_requested / feedback_queued / final_requested / recheck_requested
  任务时，立即触发并遵循本 skill。
---

# 洗片控制台（Wash Queue Console）跨会话协同协议

本工作区的桌面应用 `洗片控制台.app` 与本地服务 `server.py`（端口 `8787`，源码位于 `<workspace>/tools/wash_queue_app/` 或应用包内 `Contents/Resources/app/`）完全独立于单次对话运行；所有项目数据均持久化在当前工作区的 `wash_queue_data/` 目录（`queue.json`、`presets/` 与 `shots/`，具体路径也可从 `GET http://127.0.0.1:8787/api/state` 读取，且绝不上传或写入个人敏感路径）。

---

## 1. 无会话自动桥接（`agent_bridge.py`）与跨对话接管

> [!IMPORTANT]
> 本系统现已具备 **双模自动接单能力**：
> 1. **无会话自动桥接（默认常驻）**：打开桌面 `洗片控制台.app` 或启动本地 8787 服务时，独立守护进程 `agent_bridge.py` 会同步启动并保持心跳；即使 **没有任何活跃 Antigravity 对话**，一旦队列出现待办事件，桥接器也会自动通过本机 `agy -p` 唤醒 Worker（最多 3 路并发）按本协议端到端执行并回写。
> 2. **活跃对话优先接管**：当用户在活跃 Antigravity 对话中说“启动洗片监听 / 连上洗片控制台 / 处理排片队列”时，对话通过 `watch_queue.py --wait --mode interactive` 挂起监听；此时 `agent_bridge.py` 自动退让为热备状态，优先由当前活跃对话接单并发处理（顶栏状态灯同步显示「AI 对话已连接」或「AI 桥接已就绪」）。
>    *(注：若你是被 `agent_bridge.py` 唤醒的单镜 Worker，只需处理好当前分配到的镜头并调用 `agent-update`，**严禁**再启动 `watch_queue.py`。)*

1. **定位应用目录并检查服务是否在线**（优先使用工作区 `tools/wash_queue_app`，否则回退桌面/应用目录下的 `洗片控制台.app/Contents/Resources/app`）：
   ```bash
   APP_DIR="$( [ -d ./tools/wash_queue_app ] && pwd )/tools/wash_queue_app"
   [ -d "$APP_DIR" ] || APP_DIR="$HOME/Desktop/洗片控制台.app/Contents/Resources/app"
   [ -d "$APP_DIR" ] || APP_DIR="$HOME/Desktop/洗片控制台-同事版/洗片控制台.app/Contents/Resources/app"
   PY_BIN="$APP_DIR/.venv/bin/python"
   [ -x "$PY_BIN" ] || PY_BIN="$APP_DIR/../runtime/bin/python3.12"
   curl -sf http://127.0.0.1:8787/api/state >/dev/null || bash "$APP_DIR/launch.sh" --no-browser
   ```
2. **立即检查并签收积压的待办任务**：
   ```bash
   "$PY_BIN" "$APP_DIR/watch_queue.py" --check --claim --limit 10
   ```
   - 退出码 `0`：已一次性将待办镜头原子签收为 `processing`（每条均带独立 `claimToken` 与 `processingTaskType`），立刻按下方「多任务并发调度协议」并行派生至少 **3 路以上** `self` subagent 同时处理，并立即挂起下一轮 `--wait` 监听后续插队任务。
   - 退出码 `2`：暂无积压，挂起长期监听等待下一条事件：
     ```bash
     "$PY_BIN" "$APP_DIR/watch_queue.py" --wait --claim --limit 10
     ```
     （该命令阻塞直到 `queued` / `run_requested` / `prompt_update_requested` / `feedback_queued` / `final_requested` / `recheck_requested` 到来并自动原子签收为 `processing`，用后台 `run_command` 挂起即可，切勿轮询 sleep。）

---

## 2. 必须联动的核心 Skills 与双平台路由（LibTV / 即梦官网）

每个镜头带有 `platform` 字段（`"libtv"` 或 `"jimeng"`），请按平台分流处理：

- **当 `platform` 为 `"libtv"`（默认）**：加载并遵循 `libtv-cli`、`libtv-seedance-batch`、`seedance-video-recreation`、`seedance-video-prompts`；使用 `libtv` CLI 与 `{{Node nodeKey}}` 引用，保留网页「深度动作捕捉」流程与 `libtvMixedOrder`。
- **当 `platform` 为 `"jimeng"`（即梦官网）**：加载并遵循 `dreamina-canvas-cli`（工作区 `.agents/skills/dreamina-canvas-cli/SKILL.md` 或全局 skill）、`zdepth-video`、`seedance-video-recreation`、`seedance-video-prompts`；使用 `dreamina-canvas --non-interactive --format json` 显式传入 `--project-id <projectUuid>`，以 `{{node:<node-id>}}` 写正文引用并遵循 `dreamina-canvas-cli` 的积分报价/确认与 7 天样片有效期规则，仅将公开可恢复 ID（`nodeId` / `submitId` / `selectedResourceId`）写入 `jimengMeta`。

处理任何队列镜头前，**按任务性质同时加载并严格遵循**：
- `libtv-seedance-batch`：LibTV 画布节点、`深度动作捕捉`、Seedance 2.5（样片 480P / 正片 1080P、21:9、对齐原片整数秒）、最小必要人脸涂白（`$APP_DIR/scripts/whiteout_mask.py` 或工作区 `tools/wash_queue_app/scripts/whiteout_mask.py`）、不代点“生成”。
- `seedance-video-recreation`：多模态直读 `sourceVideo` 原片与音轨，还原镜头运动、走位方位、节拍与对白，并用简洁自然的素材绑定句声明 `@图片N` 与 `@视频N`。
- `seedance-video-prompts`：严格遵守公式结构、`这是一个一镜到底的连续长镜头，全程不切镜、不剪辑`、Dolly In 物理措辞，**影调由对应 `@图片N` 承载时不得在开头堆砌风格或重复服装细节**，结尾固定附 Arri Flex435 摄影机预设。

> [!CAUTION]
> 提示词中 **绝不允许出现** “涂白”“白底”“遮罩”“whiteout”等字样；提示词须结构精炼、动作与运镜精确，完全贴合 `seedance-video-prompts` 与 `seedance-video-recreation`。

---

## 3. 四类待办状态的处理规范

### A. `queued`（新建排片）
1. 读取 `shot` 全量元信息（`GET http://127.0.0.1:8787/api/shots/{id}` 或直接读 `watch_queue.py` 输出）：原视频 `sourceVideo`、有序图片列表 `compImage` + `charRefs`（序号即 `图片1..N`）、`whiteoutOptions`（`parts` / `targetSlot`）、`userPrompt`、`scene`。
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
按 `libtv-seedance-batch` 执行 `libtv video final -n <sampleNodeKey> -p <projectUuid> [-g <groupKey>]` 转出 1080P 正片并下载至 `shots/{shot_id}/final_1080p.mp4`，回写 `status: "final_ready"`、`finalVideo: { filePath, remoteUrl, watermark }` 与本地正片路径。

### E. `run_requested`（用户在控制台点击「▶ 帮我 Run 生成」）
当画布节点已处于 `ready_on_canvas`（已就绪）或用户改完提示词后在 App 里点击「▶ 帮我 Run 生成」：
1. 若 `pendingPromptEdit` 非空，先将最新提示词同步至 `libtvSampleNodeKey`（`libtv node <sampleNodeKey> -p <projectUuid> [-g <groupKey>] --prompt "..."`）；若尚未建完节点先按 A 流程补齐。
2. **执行生成（遵守 `libtv-cli` 规范）**：运行 `libtv node <sampleNodeKey> -p <projectUuid> [-g <groupKey>] --run` 同步阻塞等待终态 JSON（禁止自行轮询、禁止提前 kill、禁止后台化）；多路镜头并发时各自独立执行互不阻塞，且**无需占锁 Chrome UI**。
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
- 处理完毕或刚派发完并发 Worker 后，立即重新挂起 `watch_queue.py --wait --claim --limit 10`，确保后续插队任务随时并发接入。

---

## 5. 多任务并发调度协议（至少 3 路并发）

本系统支持 **>= 3 路任务真实并行执行**（Coordinator + Parallel Worker Subagents 模式）：

1. **原子领单 + 立即并行派生（Coordinator）**
   - 当一次有多个待办镜头（例如 `S04`、`S05`、`S06`），或上一批仍在运行而用户又新提交了镜头时：
     - 使用 `watch_queue.py --check --claim --limit 10`（或 `POST /api/agent/claim`）原子把当前所有可执行镜头转为 `processing` 并获取各自 `claimToken`；
     - 在**同一轮响应**中对每个已领单镜头调用一次 `invoke_subagent`（`TypeName: "self"`, `Model: "inherit"`），把 `shotId`、`processingTaskType`、`claimToken` 清晰写入各自的 Prompt，同时并发跑起（推荐并发上限 3–5 路；若积压超过 5 个，分批派生，每完成一路再补下一路）；
     - 派生完后**立刻重新挂起 `watch_queue.py --wait --claim --limit 10`**（不要等这 3 路跑完才恢复监听），从而第 4、5 个新任务一提交就能直接派生新 Worker 插队并行，不再串行排队。
2. **禁止全局状态互相踩踏（LibTV CLI 隔离规则）**
   - `.libtv/project.json` 是单文件共享状态，**并发场景下严禁调用 `libtv group use`**；
   - 所有 `libtv` 命令（`group list` / `group create` / `upload` / `node` / `seedance` / `edge` 等）**必须显式传入 `-p <projectUuid>`（以及 `-g <groupKey>` 或明确 `nodeKey`）**。
3. **重活真并行，仅对“共享 UI / 画布布局”短时加锁（~1–5 秒）**
   - **真并发执行**：`read_file` 多模态读取原片/参考图、生成 `whiteout_mask.py` 涂白图、撰写 Seedance 提示词与 `outputs/{shot_id}_提示词.md`、上传素材到 LibTV、建文本/Seedance 节点、连线、校验与下载，所有 Worker 全程互不阻塞并行执行。
   - **布局短锁（~1 秒）**：在同一张画布新建分组前，先申请画布级布局锁，查清最大右边界并创建不重叠新分组后**立即释放**：
     `POST /api/agent/locks/canvas_layout_<projectUuid>/acquire`（`{"owner": "<shotId>", "ttlSec": 45, "waitSec": 30}`）-> 建组占位 -> `release`。
   - **Chrome UI 短锁（~3–5 秒）**：只有需要操作浏览器中 LibTV 页面执行「选中视频节点 -> 点击 `深度动作捕捉` -> 点击 `确认提取`」这一瞬间时，才申请全局 `chrome_ui` 锁：
     `POST /api/agent/locks/chrome_ui/acquire`（`{"owner": "<shotId>", "ttlSec": 60, "waitSec": 45}`）。**点完 `确认提取` 见到云端任务开始跑即可立即 `release` 释放锁**（不必占用锁等云端转码完成），下一路 Worker 立刻接力点提取，云端 3 路深度视频生成同时并发推进！
4. **乐观锁回写（`claimToken` 防冲突）**
   - 每个 Worker 在执行过程中可用自己的 `claimToken` 频繁回写 `status: "processing"` 实时刷新控制台中的 `agentSummary` 进度条；
   - 最终回写 `ready_on_canvas` / `needs_confirmation` / `draft_ready` / `final_ready` 时**必须携带 `claimToken`**；若服务返回 HTTP `409 Conflict`（说明用户在你处理期间提交了新反馈或点击了“重新处理·自检”，清除了旧令牌），立即停止旧流程，绝不覆盖用户新指令。
