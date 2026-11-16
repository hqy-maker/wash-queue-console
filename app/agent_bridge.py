#!/usr/bin/env python3
"""Standalone headless Antigravity (`agy`) queue bridge for SHZ Wash Queue Console.

Runs alongside the desktop console (`server.py` on port 8787) so that even when no
interactive Antigravity conversation is attached, any queued shot, run request,
prompt sync, feedback, recheck, or 1080P final approval automatically wakes an
`agy -p` worker (up to 3 in parallel) using the workspace skills.

When an interactive Antigravity conversation is actively listening (`interactiveOnline`),
this bridge stays in warm standby so it never steals tasks from the live conversation.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from store import DATA_ROOT, WORKSPACE_ROOT, diagnose_error

HOME_DIR = Path.home()
LOCK_PATH = DATA_ROOT / "bridge.lock"
PID_PATH = DATA_ROOT / "bridge.pid"


def find_agy_bin() -> str | None:
    candidates = [
        os.environ.get("AGY_BIN", "").strip(),
        shutil.which("agy") or "",
        str(HOME_DIR / ".local" / "bin" / "agy"),
        str(HOME_DIR / ".gemini" / "antigravity" / "bin" / "agy"),
        "/Applications/Antigravity.app/Contents/Resources/app/bin/agy",
        str(HOME_DIR / "Applications" / "Antigravity.app" / "Contents/Resources/app/bin/agy"),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


AGY_BIN = find_agy_bin() or str(HOME_DIR / ".local" / "bin" / "agy")
MAX_WORKERS = max(1, min(int(os.environ.get("WASH_BRIDGE_WORKERS", "3")), 5))
AGY_TIMEOUT_SEC = max(120, int(os.environ.get("WASH_BRIDGE_AGY_TIMEOUT", "900")))

_STOP_EVENT = threading.Event()
_LOCK_FH: Any = None


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{ts}] [bridge] {msg}", flush=True)


def _acquire_singleton_lock(wait_sec: float = 3.5) -> bool:
    global _LOCK_FH
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, wait_sec)
    fh = LOCK_PATH.open("w", encoding="utf-8")
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline or _STOP_EVENT.is_set():
                fh.close()
                return False
            time.sleep(0.2)
    _LOCK_FH = fh
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return True


def _cleanup_pid_and_lock() -> None:
    try:
        if PID_PATH.is_file() and PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_PATH.unlink()
    except OSError:
        pass
    try:
        if _LOCK_FH is not None:
            fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_UN)
            _LOCK_FH.close()
    except OSError:
        pass


TASK_SECTION_HINTS = {
    "queued": "请严格按 `wash-queue-console` §3.A（queued 新建排片）执行：多模态直读原片/参考图、按需涂白、写提示词并落盘 `outputs/{shot_id}_提示词.md`、上传/深度动作捕捉/连线 LibTV（不自动点击生成），完成后回写 `ready_on_canvas` 并提供清晰的 `revisionDirection`（概括首版核心思路与保留锚点）。",
    "feedback_queued": "请严格按 `wash-queue-console` §3.B（feedback_queued 修改意见 / 涂白 / @新图 / 改词）处理，遵循 `seedance-video-prompts` 增量修改铁律（仅修改用户提及处），同步至 LibTV 节点与 `outputs/{shot_id}_提示词.md`，完成后回写 `ready_on_canvas`、置 `resolveFeedbacks: true`，并**必填** `revisionDirection`（向用户简明说明你理解的修改方向、改了哪几处、保留了哪些未动部分）。",
    "recheck_requested": "请严格按 `wash-queue-console` §3.C（recheck_requested 中断恢复与全链路自检）核对画布节点/连线/涂白/提示词/序号对齐并修复，完成后恢复合适状态并在 `revisionDirection` / `agentSummary` 汇报核查修改方向与结果。",
    "final_requested": "请严格按 `wash-queue-console` §3.D（final_requested 转 1080P 正片并下载归档）运行 `libtv video final` 并下载至 `shots/{shot_id}/final_1080p.mp4`，完成后回写 `final_ready`。",
    "run_requested": "请严格按 `wash-queue-console` §3.E（run_requested 执行 Seedance 生成并下载看片）同步待更新提示词（若有）→ 执行 `libtv node ... --run` 等待终态 → 拉回无水印 480P 样片到本地 `draft_480p.mp4` → 多模态直读拉片自检，完成后回写 `draft_ready` 与看片反馈 `revisionDirection`。",
    "prompt_update_requested": "请严格按 `wash-queue-console` §3.F（prompt_update_requested 同步手动改词/回滚词到画布，不要触发 --run）读取新旧提示词对照并更新 LibTV 画布节点与 `outputs/{shot_id}_提示词.md`，完成后恢复为原状态（`promptUpdatePrevStatus` 或合理状态）、清除 `pendingPromptEdit`，并用 `revisionDirection` 归纳本次手动调词的核心改动方向。",
}


JIMENG_TASK_SECTION_HINTS = {
    "queued": "请严格按 `wash-queue-console` + `dreamina-canvas-cli` 执行：多模态直读原片/参考图、按需涂白或用 `zdepth-video` 提取景深动作参考视频、写提示词并落盘 `outputs/{shot_id}_提示词.md`；用 `dreamina-canvas resource upload`（显式 `--project-id`）上传素材为 `node`，创建 Seedance `--mode m2v` 生成节点并通过 `{{node:<node-id>}}` 与 `--ref node:<node-id>` 连线（不自动调用 `node run` 扣费），完成后回写 `ready_on_canvas`、节点 ID、`jimengMeta` 与 `revisionDirection`。",
    "feedback_queued": "请严格按 `wash-queue-console` + `dreamina-canvas-cli` 处理即梦官网画布修改意见：遵循增量修改原则，上传新素材并通过 `dreamina-canvas node edit`（显式 `--project-id`）同步提示词与引用至即梦节点与 `outputs/{shot_id}_提示词.md`，回写 `ready_on_canvas`、`resolveFeedbacks: true` 与 `revisionDirection`。",
    "recheck_requested": "请用 `dreamina-canvas node show` 与 `schema` 核对即梦官网画布节点/连线/参考顺序/提示词规范并补全同步，恢复合适状态并在 `revisionDirection` / `agentSummary` 汇报结果。",
    "final_requested": "请用 `dreamina-canvas node promote`（若超过 7 天有效期提醒用户）或超清工作流为即梦官网样片转 1080P 正片并下载归档，回写 `final_ready`。",
    "run_requested": "请严格遵循 `dreamina-canvas-cli` 报价与积分确认规则：先核对待更新提示词并用 `dreamina-canvas node quote` 查看即梦积分报价；若需确认或无用户明确 ceiling 授权请遵循 `credits-and-hitl.md` 停下询问并保存 `jimengMeta` 中的 `submitId`；取得批准后执行 `node confirm`/`node run`、检查产物终态并交付结果（样片交付时务必提醒最长存储 7 天）。",
    "prompt_update_requested": "请对照 `promptText` 与 `previousPromptText`，通过 `dreamina-canvas node edit`（显式 `--project-id`，不自动触发 `node run`）将改词同步回即梦官网画布节点与 `outputs/{shot_id}_提示词.md`，完成后恢复为 `promptUpdatePrevStatus` 并清除 `pendingPromptEdit`。",
}


def _resolve_skill_path(skill_name: str) -> str:
    candidates = [
        WORKSPACE_ROOT / ".agents" / "skills" / skill_name / "SKILL.md",
        HOME_DIR / ".agents" / "skills" / skill_name / "SKILL.md",
        HOME_DIR / ".gemini" / "config" / "skills" / skill_name / "SKILL.md",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return str(candidates[0])


def _build_worker_prompt(shot: dict[str, Any], base_url: str) -> str:
    task_type = str(shot.get("processingTaskType") or shot.get("status") or "queued")
    platform = str(shot.get("platform") or "libtv").strip().lower()
    is_jimeng = platform == "jimeng"
    jm = shot.get("jimengMeta") or {}
    if task_type == "queued" and is_jimeng and isinstance(jm, dict) and jm.get("pendingAction") == "promote_1080p":
        task_type = "final_requested"
    wash_skill_md = _resolve_skill_path("wash-queue-console")
    dreamina_skill_md = _resolve_skill_path("dreamina-canvas-cli")
    section_hint = (
        JIMENG_TASK_SECTION_HINTS.get(task_type)
        if is_jimeng
        else TASK_SECTION_HINTS.get(task_type)
    ) or "请按 wash-queue-console 协议完整处理当前任务。"
    brief = {
        "id": shot.get("id"),
        "scene": shot.get("scene"),
        "platform": platform,
        "processingTaskType": task_type,
        "claimToken": shot.get("claimToken"),
        "projectUuid": shot.get("projectUuid"),
        "projectName": shot.get("projectName"),
        "canvasUrl": shot.get("canvasUrl"),
        "userPrompt": shot.get("userPrompt"),
        "promptText": shot.get("promptText"),
        "previousPromptText": shot.get("previousPromptText"),
        "pendingPromptEdit": shot.get("pendingPromptEdit"),
        "promptUpdatePrevStatus": shot.get("promptUpdatePrevStatus"),
        "whiteoutOptions": shot.get("whiteoutOptions"),
        "sourceVideo": (shot.get("sourceVideo") or {}).get("filePath"),
        "compImage": (shot.get("compImage") or {}).get("filePath"),
        "charRefs": shot.get("charRefs"),
        "libtv": shot.get("libtv"),
        "libtvGroupKey": shot.get("libtvGroupKey"),
        "libtvSampleNodeKey": shot.get("libtvSampleNodeKey"),
        "libtvFinalNodeKey": shot.get("libtvFinalNodeKey"),
        "libtvMixedOrder": shot.get("libtvMixedOrder"),
        "jimengMeta": shot.get("jimengMeta"),
        "promptFilePath": shot.get("promptFilePath"),
        "latestFeedback": shot.get("latestFeedback"),
    }
    if is_jimeng:
        skill_list = "（`dreamina-canvas-cli`、`zdepth-video`、`seedance-video-recreation`、`seedance-video-prompts`）"
        exec_rules = (
            f"1. 本镜头目标平台为 **即梦官网（`dreamina-canvas` CLI）**，请先阅读 `{dreamina_skill_md}`，命令入参以 `dreamina-canvas --format json schema` 实测为准，显式传入 `--project-id <projectUuid>`，不得调用未公开接口。\n"
            "2. 若需积分确认请严格遵循 `credits-and-hitl.md`，并将 `nodeId` / `submitId` / `selectedResourceId`（严禁保存 token 或凭据）写入 `jimengMeta`；向用户交付样片时须明确提醒最长存储 7 天。\n"
            f"3. 关键节点务必用 `POST {base_url}/api/shots/{shot.get('id')}/agent-update`（请求体必须带 `claimToken: \"{shot.get('claimToken')}\"`）汇报进度与最终结果；"
            "若对构图图或角色图执行了局部涂白/遮罩（例如生成了 `comp_masked.png`），**必须同步传入** `whiteoutUpdates: [{\"slotTag\": \"图片1\", \"filePath\": \"<绝对路径>\", \"parts\": [...]}]`（并在即梦平台写入 `jimengMeta.maskedCompPath`），确保控制台缩略图立即更新为涂白图并保留原图对比；"
            "修改或撰写提示词后**必须同时传入 `revisionDirection`**（1–2 句话精炼说明你理解的修改方向、改了哪几处、保留了哪些未动部分）以及 `conversationId`（若可知）；"
            "若收到 HTTP 409 Conflict 说明任务已被用户新操作取代，立刻停止。\n"
            "4. **禁止**在本 Worker 内启动 `watch_queue.py`（外层桥接进程会统一监听队列与调度并发）。\n\n"
        )
    else:
        skill_list = "（`libtv-seedance-batch`、`seedance-video-recreation`、`seedance-video-prompts`、`libtv-cli`）"
        exec_rules = (
            "1. 多路并行时严禁调用 `libtv group use`，所有 `libtv` 命令显式传 `-p <projectUuid>`（与 `-g <groupKey>`）。\n"
            "2. 只有需要点网页「深度动作捕捉」且 Chrome UI 未占锁时，才通过 `/api/agent/locks/chrome_ui/acquire` 申请短锁并在点完「确认提取」后立刻 release。\n"
            f"3. 关键节点务必用 `POST {base_url}/api/shots/{shot.get('id')}/agent-update`（请求体必须带 `claimToken: \"{shot.get('claimToken')}\"`）汇报进度与最终结果；"
            "若对构图图或角色图执行了局部涂白/遮罩（例如生成了 `comp_masked.png`），**必须同步传入** `whiteoutUpdates: [{\"slotTag\": \"图片1\", \"filePath\": \"<绝对路径>\", \"parts\": [...]}]`（并在即梦平台写入 `jimengMeta.maskedCompPath`），确保控制台缩略图立即更新为涂白图并保留原图对比；"
            "修改或撰写提示词后**必须同时传入 `revisionDirection`**（1–2 句话精炼说明你理解的修改方向、改了哪几处、保留了哪些未动部分）以及 `conversationId`（若可知）；"
            "若收到 HTTP 409 Conflict 说明任务已被用户新操作取代，立刻停止。\n"
            "4. **禁止**在本 Worker 内启动 `watch_queue.py`（外层桥接进程会统一监听队列与调度并发）。\n\n"
        )
    return (
        "你是由洗片控制台本地后台桥接器（`agent_bridge.py`）自动唤醒的 Worker Agent。\n"
        f"请先阅读 `{wash_skill_md}` 及本任务涉及的配套 Skill"
        f"{skill_list}，"
        "然后端到端处理下方这一个已被你原子签收（claimed）的镜头：\n\n"
        f"- 工作区目录：`{WORKSPACE_ROOT}`\n"
        f"- 控制台地址：`{base_url}`\n"
        f"- 目标平台：`{'即梦官网 (jimeng)' if is_jimeng else 'LibTV (libtv)'}`\n"
        f"- 镜头 ID：`{shot.get('id')}`\n"
        f"- 任务类型：`{task_type}`\n"
        f"- `claimToken`：`{shot.get('claimToken')}`\n"
        f"- 任务指引：{section_hint}\n\n"
        "## 执行硬性约束\n"
        f"{exec_rules}"
        "## 镜头完整元数据 JSON\n"
        f"```json\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n```\n"
    )


class BridgeRuntime:
    """Coordinates heartbeats, standby vs active claiming, and parallel `agy -p` execution."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._lock = threading.Lock()
        self._active: dict[str, Future[None]] = {}
        self._last_conv_id = ""
        self._last_run_at = ""
        self._last_error = ""

    def active_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._active.keys())

    def _send_heartbeat(self, client: httpx.Client) -> dict[str, Any] | None:
        ids = self.active_ids()
        with self._lock:
            last_conv = self._last_conv_id
            last_run = self._last_run_at
            last_err = self._last_error
        try:
            resp = client.post(
                "/api/agent/heartbeat",
                json={
                    "watcherId": "agy_bridge",
                    "mode": "bridge",
                    "busy": bool(ids),
                    "activeShotIds": ids,
                    "pid": os.getpid(),
                    "ttlSec": 30.0,
                    "lastConversationId": last_conv,
                    "lastRunAt": last_run,
                    "lastError": last_err,
                },
                timeout=5.0,
            )
            if resp.status_code == 200:
                return resp.json().get("agentStatus")
        except Exception:
            return None
        return None

    def _ensure_not_stuck_processing(self, shot_id: str, claim_token: str, reason: str) -> None:
        try:
            with httpx.Client(base_url=self.base_url, timeout=10.0) as client:
                r = client.get(f"/api/shots/{shot_id}")
                if r.status_code != 200:
                    return
                shot = r.json().get("shot") or {}
                if shot.get("status") == "processing" and shot.get("claimToken") == claim_token:
                    diag = diagnose_error(
                        reason,
                        failed_task=str(shot.get("processingTaskType") or ""),
                        platform=str(shot.get("platform") or ""),
                    )
                    client.post(
                        f"/api/shots/{shot_id}/agent-update",
                        json={
                            "claimToken": claim_token,
                            "status": "error",
                            "errorReason": diag["errorReason"],
                            "errorSolution": diag["errorSolution"],
                            "errorDetail": reason,
                            "agentSummary": diag["errorReason"],
                            "revisionSummary": f"agy 后台桥接异常终止：{reason}",
                        },
                    )
        except Exception as exc:
            _log(f"{shot_id} failed-state cleanup error: {exc}")

    def _run_shot_worker(self, shot: dict[str, Any]) -> None:
        shot_id = str(shot["id"])
        claim_token = str(shot.get("claimToken") or "")
        task_type = str(shot.get("processingTaskType") or "")
        _log(f"start worker shot={shot_id} task={task_type} token={claim_token}")

        prompt = _build_worker_prompt(shot, self.base_url)
        env = os.environ.copy()
        extra_path = f"{HOME_DIR / '.local' / 'bin'}:{HOME_DIR / '.libtv'}:/opt/homebrew/bin:/usr/local/bin"
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"

        conv_id = ""
        err_msg = ""
        last_status = ""
        proc: subprocess.Popen[str] | None = None
        try:
            agy_bin = find_agy_bin()
            if not agy_bin:
                raise FileNotFoundError("本机未检测到 Antigravity agy CLI（安装 Antigravity 后重试）")
            cmd = [
                agy_bin,
                "-p",
                prompt,
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--print-timeout",
                f"{AGY_TIMEOUT_SEC}s",
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            deadline = time.monotonic() + AGY_TIMEOUT_SEC + 60.0
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                if time.monotonic() > deadline:
                    err_msg = f"执行超时（超过 {AGY_TIMEOUT_SEC}s）"
                    proc.kill()
                    break
                if line.startswith("{"):
                    try:
                        evt = json.loads(line)
                        if evt.get("conversation_id") and not conv_id:
                            conv_id = str(evt["conversation_id"])
                            with self._lock:
                                self._last_conv_id = conv_id
                            _log(f"{shot_id} agy conversation_id={conv_id}")
                        if evt.get("event") == "result":
                            res_obj = evt.get("result") or {}
                            last_status = str(res_obj.get("status") or "")
                            if last_status != "SUCCESS":
                                err_msg = str(res_obj.get("error") or last_status or "unknown_error")
                    except json.JSONDecodeError:
                        pass
            ret = proc.wait(timeout=15)
            if ret != 0 and not err_msg:
                err_msg = f"agy exited with code {ret}"
        except Exception as exc:
            err_msg = str(exc)
            if proc and proc.poll() is None:
                proc.kill()
        finally:
            now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            with self._lock:
                self._active.pop(shot_id, None)
                self._last_run_at = now_str
                if conv_id:
                    self._last_conv_id = conv_id
                self._last_error = err_msg
            if err_msg or (last_status and last_status != "SUCCESS"):
                _log(f"worker finished with issue shot={shot_id}: {err_msg or last_status}")
                self._ensure_not_stuck_processing(shot_id, claim_token, err_msg or last_status)
            else:
                _log(f"worker finished shot={shot_id} conv={conv_id}")

    def run_forever(self) -> int:
        if not find_agy_bin():
            _log("WARNING: Antigravity agy CLI not found; waiting in standby")

        pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="agy-worker")

        def heartbeat_loop() -> None:
            with httpx.Client(base_url=self.base_url, timeout=8.0) as hb_client:
                while not _STOP_EVENT.is_set():
                    self._send_heartbeat(hb_client)
                    _STOP_EVENT.wait(5.0)

        hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        hb_thread.start()

        since_seq = 0
        try:
            with httpx.Client(base_url=self.base_url, timeout=30.0) as client:
                while not _STOP_EVENT.is_set():
                    try:
                        status_resp = client.get("/api/agent/status", timeout=5.0)
                        if status_resp.status_code != 200:
                            _STOP_EVENT.wait(2.0)
                            continue
                        st = status_resp.json()
                        if st.get("bridgePaused"):
                            _STOP_EVENT.wait(2.0)
                            continue
                        if st.get("interactiveOnline"):
                            _STOP_EVENT.wait(2.0)
                            continue

                        with self._lock:
                            running_count = len(self._active)
                        slots = MAX_WORKERS - running_count

                        if slots > 0 and st.get("pendingCount", 0) > 0 and find_agy_bin():
                            claim_resp = client.post(
                                "/api/agent/claim",
                                json={"workerId": "agy_bridge", "limit": slots},
                                timeout=8.0,
                            )
                            if claim_resp.status_code == 200:
                                claimed = claim_resp.json().get("claimed") or []
                                if claimed:
                                    with self._lock:
                                        for s in claimed:
                                            sid = str(s["id"])
                                            if sid in self._active:
                                                continue
                                            fut = pool.submit(self._run_shot_worker, s)
                                            self._active[sid] = fut
                                    self._send_heartbeat(client)
                                    continue

                        wait_resp = client.get(
                            "/api/events/wait",
                            params={
                                "since": since_seq,
                                "timeout": 2.0,
                                "actionable": "true",
                                "watcher": "agy_bridge",
                                "mode": "bridge",
                            },
                            timeout=6.0,
                        )
                        if wait_resp.status_code == 200:
                            since_seq = int(wait_resp.json().get("eventSeq", since_seq))
                    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                        _STOP_EVENT.wait(2.0)
                    except Exception as exc:
                        _log(f"loop error: {exc}")
                        _STOP_EVENT.wait(2.0)
        finally:
            pool.shutdown(wait=False, cancel_futures=False)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless agy queue bridge for SHZ Wash Queue Console")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WASH_APP_PORT", "8787")))
    args = parser.parse_args()

    if not _acquire_singleton_lock():
        _log("another bridge instance is already active; exiting")
        return 0

    def _on_signal(_signum: int, _frame: Any) -> None:
        _STOP_EVENT.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    base_url = f"http://127.0.0.1:{args.port}"
    _log(f"started pid={os.getpid()} target={base_url} max_workers={MAX_WORKERS}")
    try:
        runtime = BridgeRuntime(base_url)
        return runtime.run_forever()
    finally:
        _cleanup_pid_and_lock()
        _log("stopped")


if __name__ == "__main__":
    raise SystemExit(main())
