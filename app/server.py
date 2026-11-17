from __future__ import annotations

import json
import mimetypes
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent_bridge
import jimeng_bridge
import libtv_bridge
import shot_fastpath
from updater import update_manager
from store import (
    ACTIONABLE_STATUSES,
    APP_ROOT,
    DATA_ROOT,
    SHOTS_DIR,
    TASK_TYPE_LABELS,
    WORKSPACE_ROOT,
    ClaimConflictError,
    QueueStore,
    build_canvas_url,
    get_platform_label,
    is_fastpath_mechanical_candidate,
    normalize_ai_dispatch_mode,
    normalize_platform,
    resolve_desktop_conversation_id,
    run_preflight,
    sanitize_filename,
)

HOME_DIR = Path.home()
BRIDGE_SCRIPT = APP_ROOT / "agent_bridge.py"
BRIDGE_PID_PATH = DATA_ROOT / "bridge.pid"
BRIDGE_LOG_PATH = DATA_ROOT / "bridge.log"
BRIDGE_PAUSED_FLAG = DATA_ROOT / ".bridge_paused"


def _read_bridge_pid() -> int | None:
    if not BRIDGE_PID_PATH.is_file():
        return None
    try:
        pid = int(BRIDGE_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def _is_agy_command_pid(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        proc = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2)
        cmd = (proc.stdout or "").strip().lower()
        return "agy" in cmd
    except Exception:
        return True


def _parse_iso_ts(val: str | None) -> float | None:
    text = str(val or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).timestamp()
        except ValueError:
            continue
    return None


def _tail_shot_bridge_logs(shot_id: str, limit: int = 8) -> list[str]:
    if not BRIDGE_LOG_PATH.is_file():
        return []
    try:
        size = BRIDGE_LOG_PATH.stat().st_size
        with BRIDGE_LOG_PATH.open("rb") as fh:
            if size > 32768:
                fh.seek(-32768, os.SEEK_END)
            raw = fh.read().decode("utf-8", errors="ignore")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        needle_eq = f"shot={shot_id}"
        needle_sp = f"] {shot_id} "
        matched = [ln for ln in lines if needle_eq in ln or needle_sp in ln or f" {shot_id} " in ln]
        return matched[-limit:]
    except Exception:
        return []


def _is_antigravity_app_installed() -> bool:
    return any(
        cand.exists()
        for cand in (
            Path("/Applications/Antigravity.app"),
            HOME_DIR / "Applications" / "Antigravity.app",
        )
    )


def _build_desktop_coordinator_guide() -> dict[str, Any]:
    port = int(os.environ.get("WASH_APP_PORT", "8787"))
    base_url = f"http://127.0.0.1:{port}"
    py_bin = sys.executable or "python3"
    wait_script = str((APP_ROOT / "scripts" / "wait_next_ai_task.py").resolve())
    port_arg = f" --base-url {base_url}" if port != 8787 else ""
    wait_cmd = f"{shlex.quote(py_bin)} {shlex.quote(wait_script)}{port_arg} --timeout 28800"
    prompt_text = (
        "请读取 wash-queue-console 协议，专职担任洗片控制台桌面主控：\n"
        f"1. 调用 POST {base_url}/api/config/ai-dispatch-mode 将模式设为 desktop_subagent；\n"
        f"2. 挂起运行 {wait_cmd} 保持监听；\n"
        f"3. 收到待办后按 1~2 路并发调用 {base_url}/api/agent/coordinator-claim 领单，"
        "为每个镜头开专属 self 子对话并调用 "
        f"{base_url}/api/shots/{{id}}/bind-conversation 绑定，"
        "后续同镜返工用 send_message 续接；处理完成后立刻重启 wait_next_ai_task.py 继续监听。"
    )
    return {
        "baseUrl": base_url,
        "workspaceRoot": str(WORKSPACE_ROOT),
        "appRoot": str(APP_ROOT),
        "pythonBin": py_bin,
        "waitScript": wait_script,
        "waitCommand": wait_cmd,
        "promptText": prompt_text,
        "shortTrigger": "启动洗片监听",
        "antigravityInstalled": _is_antigravity_app_installed(),
    }


class AgentPresenceTracker:
    """In-memory heartbeat & live per-shot runtime telemetry for the agy bridge pool."""

    def __init__(self) -> None:
        # Use a non-reentrant lock and never acquire store locks while holding it.
        self._lock = threading.Lock()
        # Leaf lock strictly for _active_desktop_token so store.wait_for_events abort_check never nests locks.
        self._token_lock = threading.Lock()
        self._watchers: dict[str, dict[str, Any]] = {}
        self._active_desktop_token: str = ""
        self._shot_workers: dict[str, dict[str, Any]] = {}
        self._cancel_tokens: dict[str, float] = {}
        self._bridge_meta: dict[str, Any] = {
            "lastConversationId": "",
            "lastRunAt": "",
            "lastError": "",
            "busy": False,
            "activeShotIds": [],
        }
        self._desktop_meta: dict[str, Any] = {
            "lastConversationId": "",
            "lastRunAt": "",
            "lastError": "",
            "busy": False,
            "activeShotIds": [],
        }

    def is_paused(self) -> bool:
        return BRIDGE_PAUSED_FLAG.is_file()

    def set_paused(self, paused: bool) -> None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        if paused:
            BRIDGE_PAUSED_FLAG.write_text("1\n", encoding="utf-8")
        elif BRIDGE_PAUSED_FLAG.is_file():
            BRIDGE_PAUSED_FLAG.unlink()

    def _prune_locked(self, now: float) -> None:
        expired = [k for k, v in self._watchers.items() if now >= float(v.get("expiresAt", 0.0))]
        for k in expired:
            self._watchers.pop(k, None)
        if not any(w.get("mode") == "desktop" for w in self._watchers.values()):
            self._desktop_meta["busy"] = False
            self._desktop_meta["activeShotIds"] = []
        if not any(w.get("mode") == "bridge" for w in self._watchers.values()):
            self._bridge_meta["busy"] = False
            self._bridge_meta["activeShotIds"] = []
        expired_workers = [
            sid for sid, w in self._shot_workers.items() if now - float(w.get("heartbeatMono", now)) >= 45.0
        ]
        for sid in expired_workers:
            self._shot_workers.pop(sid, None)
        expired_cancels = [tok for tok, exp in self._cancel_tokens.items() if now >= exp]
        for tok in expired_cancels:
            self._cancel_tokens.pop(tok, None)

    def request_cancel(self, shot_id: str, claim_token: str) -> dict[str, Any]:
        clean_tok = str(claim_token or "").strip()
        killed_pid: int | None = None
        agy_pid: int | None = None
        now = time.monotonic()
        with self._lock:
            w = self._shot_workers.get(shot_id)
            if isinstance(w, dict):
                w_pid = w.get("agyPid")
                if isinstance(w_pid, int) and (not clean_tok or str(w.get("claimToken") or "") == clean_tok):
                    agy_pid = w_pid
                self._shot_workers.pop(shot_id, None)
            if clean_tok:
                self._cancel_tokens[clean_tok] = now + 90.0
        if agy_pid and _is_agy_command_pid(agy_pid):
            try:
                os.kill(agy_pid, signal.SIGTERM)
                killed_pid = agy_pid
            except OSError:
                pass
        return {"cancelledToken": clean_tok, "killedPid": killed_pid}

    def get_active_cancel_tokens(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            return list(self._cancel_tokens.keys())

    def _get_active_desktop_token(self) -> str:
        with self._token_lock:
            return self._active_desktop_token

    def is_desktop_token_superseded(self, token: str) -> bool:
        clean = (token or "").strip()
        if not clean:
            return False
        with self._token_lock:
            return bool(self._active_desktop_token and clean != self._active_desktop_token)

    def touch_desktop_activity(
        self,
        *,
        active_shot_ids: list[str] | None = None,
        ttl_sec: float = 600.0,
    ) -> None:
        if active_shot_ids is not None:
            eff_ids = list(active_shot_ids)
        else:
            with self._lock:
                eff_ids = list(self._desktop_meta.get("activeShotIds") or [])
        # Call outside self._lock so record_heartbeat / store never nest locks.
        self.record_heartbeat(
            "desktop_coordinator",
            "desktop",
            busy=bool(eff_ids),
            active_shot_ids=eff_ids,
            ttl_sec=ttl_sec,
            return_snapshot=False,
        )

    def record_heartbeat(
        self,
        watcher_id: str,
        mode: str = "bridge",
        *,
        busy: bool = False,
        active_shot_ids: list[str] | None = None,
        pid: int | None = None,
        ttl_sec: float = 40.0,
        last_conversation_id: str | None = None,
        last_run_at: str | None = None,
        last_error: str | None = None,
        shot_workers: dict[str, dict[str, Any]] | None = None,
        watcher_token: str | None = None,
        replace_token: bool = False,
        return_snapshot: bool = True,
    ) -> dict[str, Any]:
        clean_mode = "desktop" if str(mode or "").strip().lower() == "desktop" else "bridge"
        default_wid = "desktop_coordinator" if clean_mode == "desktop" else "agy_bridge"
        wid = (watcher_id or default_wid).strip()[:80] or default_wid
        max_ttl = 900.0 if clean_mode == "desktop" else 180.0
        ttl = max(5.0, min(float(ttl_sec or 40.0), max_ttl))
        now = time.monotonic()
        wall_now = time.time()
        active_clean = [str(x).strip() for x in (active_shot_ids or []) if str(x).strip()]
        superseded = False
        token_replaced = False
        with self._lock:
            self._prune_locked(now)
            prev = self._watchers.get(wid) or {}
            eff_pid = int(pid) if pid else prev.get("pid")
            eff_tok = ""
            if clean_mode == "desktop":
                clean_tok = (watcher_token or "").strip()
                with self._token_lock:
                    cur_tok = self._active_desktop_token
                    if clean_tok:
                        if replace_token or not cur_tok:
                            if cur_tok and cur_tok != clean_tok:
                                token_replaced = True
                            self._active_desktop_token = clean_tok
                            cur_tok = clean_tok
                        elif clean_tok != cur_tok:
                            superseded = True
                    eff_tok = clean_tok or str(prev.get("watcherToken") or cur_tok or "")
            if not superseded:
                self._watchers[wid] = {
                    "watcherId": wid,
                    "mode": clean_mode,
                    "busy": bool(busy),
                    "activeShotIds": active_clean,
                    "pid": eff_pid,
                    "watcherToken": eff_tok,
                    "expiresAt": now + ttl,
                    "updatedAt": wall_now,
                }
                meta = self._desktop_meta if clean_mode == "desktop" else self._bridge_meta
                meta["busy"] = bool(busy)
                meta["activeShotIds"] = active_clean
                if last_conversation_id is not None:
                    meta["lastConversationId"] = str(last_conversation_id).strip()
                if last_run_at is not None:
                    meta["lastRunAt"] = str(last_run_at).strip()
                if last_error is not None:
                    meta["lastError"] = str(last_error).strip()
                if isinstance(shot_workers, dict):
                    next_workers = {
                        sid: rec
                        for sid, rec in self._shot_workers.items()
                        if str(rec.get("surface") or "bridge") != clean_mode
                    }
                    for k, v in shot_workers.items():
                        if not isinstance(v, dict):
                            continue
                        sid = str(v.get("shotId") or k).strip()
                        if not sid:
                            continue
                        rec = dict(v)
                        rec["shotId"] = sid
                        rec["surface"] = clean_mode
                        rec["heartbeatMono"] = now
                        rec["heartbeatWall"] = wall_now
                        next_workers[sid] = rec
                    self._shot_workers = next_workers
        if token_replaced:
            store.notify_waiters()
        if not return_snapshot:
            return {}
        return self.snapshot()

    def clear_watcher(self, watcher_id: str) -> None:
        wid = (watcher_id or "").strip()
        if not wid:
            return
        with self._lock:
            removed = self._watchers.pop(wid, None)
            surface = str((removed or {}).get("mode") or ("desktop" if wid.startswith("desktop") else "bridge"))
            if wid == "agy_bridge" or wid.startswith("desktop"):
                self._shot_workers = {
                    sid: rec
                    for sid, rec in self._shot_workers.items()
                    if str(rec.get("surface") or "bridge") != surface
                }

    def inspect_shot_runtime(self, shot: dict[str, Any]) -> dict[str, Any]:
        sid = str(shot.get("id") or "").strip()
        status = str(shot.get("status") or "").strip()
        task_type = str(shot.get("processingTaskType") or (status if status in ACTIONABLE_STATUSES else "") or "").strip()
        task_label = TASK_TYPE_LABELS.get(task_type, task_type or "后台处理")
        snap = self.snapshot()
        bridge_online = bool(snap.get("bridgeOnline"))
        bridge_paused = bool(snap.get("bridgePaused"))
        bridge_pid = snap.get("bridgePid")
        ai_mode = str(snap.get("aiDispatchMode") or store.get_ai_dispatch_mode())
        desktop_online = bool(snap.get("desktopCoordinatorOnline"))
        claimed_by = str(shot.get("claimedBy") or "").strip()
        is_fastpath_cand = is_fastpath_mechanical_candidate(shot)
        desktop_conv_id = resolve_desktop_conversation_id(shot)
        wall_now = time.time()

        with self._lock:
            self._prune_locked(time.monotonic())
            worker = dict(self._shot_workers.get(sid) or {})

        claim_tok = str(shot.get("claimToken") or "").strip()
        if worker and claim_tok and str(worker.get("claimToken") or "").strip() not in {"", claim_tok}:
            worker = {}

        claimed_ts = _parse_iso_ts(shot.get("claimedAt") or shot.get("updatedAt"))
        started_at = float(worker.get("startedAt") or claimed_ts or wall_now) if (worker or status == "processing") else None
        last_event_at = float(worker.get("lastEventAt") or started_at or wall_now) if (worker or status == "processing") else None
        elapsed_sec = max(0, int(round(wall_now - started_at))) if started_at is not None else 0
        idle_sec = max(0, int(round(wall_now - last_event_at))) if last_event_at is not None else 0

        agy_pid = worker.get("agyPid") if isinstance(worker.get("agyPid"), int) else None
        agy_pid_alive = _is_agy_command_pid(agy_pid) if agy_pid else None

        is_processing = status == "processing"
        is_waiting = status in ACTIONABLE_STATUSES
        health = "idle"
        health_label = "当前不在执行队列中"
        diagnosis = "该镜头目前未在队列或后台处理中。"
        suggested_action = "none"

        if is_processing:
            if claimed_by == "desktop_coordinator" or (ai_mode == "desktop_subagent" and claimed_by != "agy_bridge" and not worker):
                if not desktop_online and elapsed_sec >= 300:
                    health = "stalled"
                    health_label = "🟡 桌面协同监听已离线（耗时较长）"
                    diagnosis = f"该镜头已由桌面一镜一对话接管（{elapsed_sec} 秒），但桌面监听脚本已超过 {elapsed_sec} 秒未上报；如已中断可点击「↻ 刷新重试」。"
                    suggested_action = "retry"
                elif elapsed_sec >= 600:
                    health = "slow"
                    health_label = f"🟠 桌面单镜子对话已运行 {elapsed_sec} 秒"
                    diagnosis = "该镜头专属子对话仍在执行中（可能包含出片或复杂排查）；可在 Antigravity 桌面端查看对应子对话，或点「↻ 刷新重试」。"
                    suggested_action = "wait"
                else:
                    health = "running"
                    health_label = "🟢 桌面单镜子对话处理中"
                    diagnosis = f"正在 Antigravity 桌面端专属子对话中处理（已耗时 {elapsed_sec} 秒），完成后将自动回写队列状态。"
                    suggested_action = "wait"
            elif not bridge_online:
                health = "stalled"
                health_label = "🔴 后台执行器进程未运行"
                diagnosis = "镜头仍标记为「处理中」，但后台执行器进程已离线；建议点「↻ 刷新重试」重新入队并启动执行。"
                suggested_action = "retry"
            elif not worker:
                if elapsed_sec >= 12:
                    health = "stalled"
                    health_label = "🔴 未检测到活跃 Worker（疑似中断）"
                    diagnosis = f"执行器虽在线，但已过 {elapsed_sec} 秒仍未找到该镜头的活跃 Worker（可能进程曾重启中断）；点「↻ 刷新重试」可立刻重跑。"
                    suggested_action = "retry"
                else:
                    health = "running"
                    health_label = "🟢 Worker 刚签收启动中"
                    diagnosis = f"任务已签收（{elapsed_sec} 秒），正在拉起 Worker 状态心跳…"
                    suggested_action = "wait"
            elif agy_pid and agy_pid_alive is False:
                health = "stalled"
                health_label = "🔴 子进程已退出（未正常回写）"
                diagnosis = f"检测到子进程 PID {agy_pid} 已不在运行，但镜头状态尚未回写完成；请点击「↻ 刷新重试」。"
                suggested_action = "retry"
            elif idle_sec >= 180:
                health = "stalled"
                health_label = f"🟠 已 {idle_sec} 秒无任何新响应（疑似卡住）"
                diagnosis = f"已累计耗时 {elapsed_sec} 秒，但超过 {idle_sec} 秒没有任何新动作输出，可能卡在浏览器弹窗、登录或平台超时；可直接点「↻ 刷新重试」。"
                suggested_action = "retry"
            elif idle_sec >= 60:
                health = "slow"
                health_label = f"🟡 等待平台/工具响应中（{idle_sec} 秒前更新）"
                diagnosis = f"Worker 进程在线（已耗时 {elapsed_sec} 秒），正在等待平台出片或外部工具返回结果。"
                suggested_action = "wait"
            else:
                health = "running"
                health_label = "🟢 Agent 正在正常处理中"
                diagnosis = f"Worker 活跃运行中，已耗时 {elapsed_sec} 秒，最近 {idle_sec} 秒内有新输出。"
                suggested_action = "wait"
        elif is_waiting:
            if ai_mode == "desktop_subagent" and not is_fastpath_cand:
                if desktop_online:
                    health = "queued"
                    health_label = "⏳ 等待桌面一镜一对话接单"
                    diagnosis = "当前处于「桌面一镜一对话模式」，Antigravity 桌面主控将按 1~2 路并发为该镜头开启或续接专属子对话。"
                    suggested_action = "wait"
                else:
                    health = "offline"
                    health_label = "🟡 等待桌面协同监听上线"
                    diagnosis = "当前为「桌面一镜一对话模式」，需 AI 处理的镜头由桌面主控监听派发；可等待桌面主控接单，或切换至「独立 CLI 桥接」。"
                    suggested_action = "wait"
            elif bridge_paused:
                health = "paused"
                health_label = "⏸ 后台执行器已暂停"
                diagnosis = f"后台 {snap.get('maxWorkers', 2)} 路并发执行器处于暂停状态，任务暂时不会自动领单；点击右上角状态恢复执行器或点「↻ 重试」核查。"
                suggested_action = "resume_bridge"
            elif not bridge_online:
                health = "offline"
                health_label = "🔴 后台执行器未连接"
                diagnosis = "当前任务正在队列等待，但后台执行器尚未启动；请在右上角点击「重启本地执行器」。"
                suggested_action = "resume_bridge"
            else:
                health = "queued"
                health_label = "⏳ 正在排队等待空闲槽位领单"
                active_cnt = len(snap.get("activeShotIds") or [])
                diagnosis = f"后台执行器正常在线（当前正在处理 {active_cnt}/{snap.get('maxWorkers', 2)} 个镜头），空闲后会立即自动领单执行。"
                suggested_action = "wait"

        return {
            "shotId": sid,
            "status": status,
            "isProcessing": is_processing,
            "isWaiting": is_waiting,
            "taskType": task_type,
            "taskLabel": task_label,
            "health": health,
            "healthLabel": health_label,
            "diagnosis": diagnosis,
            "suggestedAction": suggested_action,
            "elapsedSec": elapsed_sec,
            "idleSec": idle_sec,
            "phase": str(worker.get("phase") or ("排队待领单" if is_waiting else ("桌面子对话处理中" if (claimed_by == "desktop_coordinator" and is_processing) else ("后台处理中" if is_processing else "")))),
            "stepCount": int(worker.get("stepCount") or 0),
            "stepType": str(worker.get("stepType") or ""),
            "lastNote": str(worker.get("lastNote") or shot.get("agentSummary") or ""),
            "recentSteps": list(worker.get("recentSteps") or []),
            "model": str(worker.get("model") or ""),
            "resumed": bool(worker.get("resumed")),
            "conversationId": str(worker.get("conversationId") or desktop_conv_id or shot.get("cliConversationId") or ""),
            "desktopConversationId": desktop_conv_id,
            "desktopOnline": desktop_online,
            "aiDispatchMode": ai_mode,
            "agyPid": agy_pid,
            "agyPidAlive": agy_pid_alive,
            "bridgeOnline": bridge_online,
            "bridgePaused": bridge_paused,
            "bridgePid": bridge_pid,
            "claimedAt": str(shot.get("claimedAt") or ""),
            "claimedBy": claimed_by,
            "startedAtIso": str(worker.get("startedAtIso") or shot.get("claimedAt") or ""),
            "lastEventAtIso": str(worker.get("lastEventAtIso") or ""),
            "recentLogs": _tail_shot_bridge_logs(sid, limit=8),
            "checkedAt": time.strftime("%H:%M:%S", time.localtime(wall_now)),
        }

    def snapshot(self, state_data: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.monotonic()
        wall_now = time.time()
        live_pid = _read_bridge_pid()
        paused = self.is_paused()
        counts_fallback = store.get_runtime_counts() if state_data is None else None
        ai_mode = normalize_ai_dispatch_mode(
            (state_data or {}).get("aiDispatchMode")
            if state_data is not None
            else (counts_fallback or {}).get("aiDispatchMode")
        )
        with self._lock:
            self._prune_locked(now)
            watchers = list(self._watchers.values())
            bridge_watchers = [w for w in watchers if w.get("mode") == "bridge"]
            desktop_watchers = [w for w in watchers if w.get("mode") == "desktop"]
            bridge_online = bool(bridge_watchers) or (live_pid is not None and not paused)
            desktop_online = bool(desktop_watchers)
            bridge_meta = dict(self._bridge_meta)
            desktop_meta = dict(self._desktop_meta)
            workers_copy = {sid: dict(w) for sid, w in self._shot_workers.items()}

        processing_ids: list[str] = []
        pending_count = 0
        ai_pending_count = 0
        fastpath_pending_count = 0
        if state_data:
            shots = state_data.get("shots")
            if isinstance(shots, list):
                processing_ids = [s["id"] for s in shots if s.get("status") == "processing"]
                for s in shots:
                    if s.get("status") in ACTIONABLE_STATUSES:
                        pending_count += 1
                        if is_fastpath_mechanical_candidate(s):
                            fastpath_pending_count += 1
                        else:
                            ai_pending_count += 1
            else:
                processing_ids = list(state_data.get("processingShotIds") or [])
                pending_count = int(state_data.get("pendingCount", 0))
                ai_pending_count = int(state_data.get("aiPendingCount", 0))
                fastpath_pending_count = int(state_data.get("fastpathPendingCount", 0))
        elif counts_fallback:
            processing_ids = list(counts_fallback.get("processingShotIds") or [])
            pending_count = int(counts_fallback.get("pendingCount", 0))
            ai_pending_count = int(counts_fallback.get("aiPendingCount", 0))
            fastpath_pending_count = int(counts_fallback.get("fastpathPendingCount", 0))

        bridge_active_ids = bridge_meta.get("activeShotIds") or []
        desktop_active_ids = desktop_meta.get("activeShotIds") or []
        all_active_ids = sorted(set(processing_ids) | set(bridge_active_ids) | set(desktop_active_ids))
        any_busy = bool(all_active_ids or bridge_meta.get("busy") or desktop_meta.get("busy"))

        agy_installed = bool(agent_bridge.find_agy_bin())
        max_workers = int(getattr(agent_bridge, "MAX_WORKERS", 2) or 2)
        if ai_mode == "desktop_subagent":
            if any_busy:
                conn_state = "busy"
                busy_count = len(all_active_ids) or 1
                label = f"一镜一对话处理中 ({busy_count})"
                detail = (
                    f"桌面一镜一对话模式：正在处理 {busy_count} 个镜头（{', '.join(all_active_ids)}）。"
                    "需 AI 思考任务由桌面专属子对话处理（1~2 并发），纯机械操作由本地 Fast-Path 直出。"
                )
            elif paused:
                conn_state = "paused"
                label = "本地 Fast-Path 已暂停"
                detail = "本地 Fast-Path 执行器已手动暂停；点击「重启本地执行器」可恢复零 LLM 改词/出片直出。"
            elif desktop_online and bridge_online:
                conn_state = "bridge_ready"
                label = "桌面一镜一对话在线"
                detail = (
                    "桌面一镜一对话与本地 Fast-Path 均在线：首次分析/返工/复检由 Antigravity 桌面主控开专属子对话执行，"
                    "改词/出样片/转 1080P 由本地零 LLM 极速直出（无独立 CLI 登录弹窗）。"
                )
            elif desktop_online:
                conn_state = "bridge_ready"
                label = "桌面在线（Fast-Path 离线）"
                detail = "桌面一镜一对话监听在线；本地 Fast-Path 执行器暂未启动，点击下方重启即可启用零 LLM 极速直出。"
            elif bridge_online:
                conn_state = "bridge_ready"
                label = "一镜一对话 · 待桌面监听"
                detail = (
                    "本地 Fast-Path 执行器在线（可零 LLM 直出即梦改词/样片/1080P）；"
                    "桌面监听脚本暂未挂载，请在 Antigravity 桌面主控中开启监听即可自动派发单镜子对话。"
                )
            else:
                conn_state = "offline"
                label = "桌面一镜一对话未连接"
                detail = "桌面监听与本地 Fast-Path 执行器均未运行；可点击下方启动本地执行器并在桌面端开启监听。"
        else:
            if any_busy and bridge_online:
                conn_state = "busy"
                busy_count = len(set(processing_ids) | set(bridge_active_ids)) or 1
                label = f"AI 并发处理中 ({busy_count})"
                detail = f"独立 CLI 桥接正在并发处理 {busy_count} 个镜头（{', '.join(processing_ids or bridge_active_ids)}）。"
            elif paused:
                conn_state = "paused"
                label = "AI 桥接已暂停"
                detail = f"后台 {max_workers} 路 CLI 桥接已手动暂停；点击下方「一键重连（启动执行器）」即可恢复。"
            elif bridge_online and not agy_installed:
                conn_state = "bridge_ready"
                label = "桥接待配置 CLI"
                detail = "当前为独立 CLI 桥接模式，但未检测到 `agy` 命令行工具；建议切换回「桌面一镜一对话」模式。"
            elif bridge_online:
                conn_state = "bridge_ready"
                label = "独立 CLI 桥接就绪"
                detail = f"后台 {max_workers} 路并发 CLI 桥接在线（agy CLI）；每个镜头独立 CLI 会话互不阻塞。"
            else:
                conn_state = "offline"
                label = "AI 桥接未连接"
                detail = "后台并发桥接进程未运行；点击下方「一键重连（启动执行器）」即可恢复。"

        worker_summaries: dict[str, dict[str, Any]] = {}
        for sid, w in workers_copy.items():
            started_at = float(w.get("startedAt") or wall_now)
            last_ev_at = float(w.get("lastEventAt") or started_at)
            worker_summaries[sid] = {
                "phase": w.get("phase", ""),
                "lastNote": w.get("lastNote", ""),
                "elapsedSec": max(0, int(round(wall_now - started_at))),
                "idleSec": max(0, int(round(wall_now - last_ev_at))),
                "stepCount": int(w.get("stepCount") or 0),
                "agyPid": w.get("agyPid"),
            }

        return {
            "state": conn_state,
            "label": label,
            "detail": detail,
            "aiDispatchMode": ai_mode,
            "desktopCoordinatorOnline": desktop_online,
            "desktopActiveShotIds": desktop_active_ids,
            "desktopWatcherPid": next((w.get("pid") for w in desktop_watchers if w.get("pid")), None),
            "desktopWatcherToken": self._get_active_desktop_token() if desktop_online else "",
            "desktopLastHeartbeatAt": max((float(w.get("updatedAt") or 0.0) for w in desktop_watchers), default=0.0),
            "aiPendingCount": ai_pending_count,
            "fastpathPendingCount": fastpath_pending_count,
            "agyInstalled": agy_installed,
            "bridgeOnline": bridge_online,
            "bridgePid": live_pid,
            "bridgePaused": paused,
            "maxWorkers": max_workers,
            "busy": any_busy,
            "activeShotIds": all_active_ids,
            "processingShotIds": processing_ids,
            "pendingCount": pending_count,
            "lastConversationId": bridge_meta.get("lastConversationId", ""),
            "lastRunAt": desktop_meta.get("lastRunAt") or bridge_meta.get("lastRunAt", ""),
            "lastError": desktop_meta.get("lastError") or bridge_meta.get("lastError", ""),
            "shotWorkers": worker_summaries,
            "desktopCoordinatorGuide": _build_desktop_coordinator_guide(),
        }


agent_presence = AgentPresenceTracker()
_BRIDGE_PROC_LOCK = threading.Lock()
_BRIDGE_CHILDREN: list[subprocess.Popen[Any]] = []
_BRIDGE_LAST_SPAWN_AT = 0.0


def _reap_bridge_children_locked() -> int | None:
    global _BRIDGE_CHILDREN
    alive: list[subprocess.Popen[Any]] = []
    running_pid: int | None = None
    for proc in _BRIDGE_CHILDREN:
        if proc.poll() is None:
            alive.append(proc)
            running_pid = proc.pid
    _BRIDGE_CHILDREN = alive
    return running_pid


def ensure_bridge_running(*, clear_paused: bool = False) -> dict[str, Any]:
    global _BRIDGE_LAST_SPAWN_AT
    with _BRIDGE_PROC_LOCK:
        if clear_paused:
            agent_presence.set_paused(False)
        elif agent_presence.is_paused():
            return {"ok": True, "started": False, "reason": "paused", "pid": _read_bridge_pid()}

        child_pid = _reap_bridge_children_locked()
        existing_pid = child_pid or _read_bridge_pid()
        if existing_pid is not None:
            return {"ok": True, "started": False, "alreadyRunning": True, "pid": existing_pid}

        if not BRIDGE_SCRIPT.is_file():
            return {"ok": False, "started": False, "error": f"缺少桥接脚本: {BRIDGE_SCRIPT}"}

        now = time.monotonic()
        if now - _BRIDGE_LAST_SPAWN_AT < 1.0:
            return {"ok": True, "started": False, "reason": "spawning", "pid": None}

        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        extra_path = f"{HOME_DIR / '.local' / 'bin'}:{HOME_DIR / '.libtv'}:{APP_ROOT / 'bin'}:/opt/homebrew/bin:/usr/local/bin"
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
        env["WASH_APP_PORT"] = str(env.get("WASH_APP_PORT") or "8787")
        with BRIDGE_LOG_PATH.open("a", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(
                [sys.executable, str(BRIDGE_SCRIPT), "--port", env["WASH_APP_PORT"]],
                cwd=str(APP_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
                close_fds=True,
            )
        _BRIDGE_CHILDREN.append(proc)
        _BRIDGE_LAST_SPAWN_AT = time.monotonic()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if BRIDGE_PID_PATH.is_file() and _read_bridge_pid() is not None:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return {"ok": True, "started": True, "pid": proc.pid}


def stop_bridge_process(*, pause: bool = True) -> dict[str, Any]:
    with _BRIDGE_PROC_LOCK:
        if pause:
            agent_presence.set_paused(True)
        child_pid = _reap_bridge_children_locked()
        pid = child_pid or _read_bridge_pid()
        stopped = False
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped = True
            except OSError:
                stopped = False
            deadline = time.monotonic() + 2.6
            while time.monotonic() < deadline:
                _reap_bridge_children_locked()
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
        if BRIDGE_PID_PATH.is_file() and _read_bridge_pid() is None:
            try:
                BRIDGE_PID_PATH.unlink()
            except OSError:
                pass
        agent_presence.clear_watcher("agy_bridge")
        return {"ok": True, "stopped": stopped, "pid": pid}


def restart_bridge_process() -> dict[str, Any]:
    snap = agent_presence.snapshot(store.get_state())
    if snap.get("busy") and snap.get("bridgeOnline"):
        active = "、".join(snap.get("activeShotIds") or [])
        raise HTTPException(
            status_code=409,
            detail=f"后台桥接正在处理镜头（{active or '运行中'}），请等待当前任务完成后再重连。",
        )
    stop_res = stop_bridge_process(pause=False)
    start_res = ensure_bridge_running(clear_paused=True)
    return {**start_res, "restarted": True, "previousPid": stop_res.get("pid")}


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    stop_supervisor = threading.Event()

    def _supervisor_loop() -> None:
        while not stop_supervisor.is_set():
            if os.environ.get("WASH_AUTO_BRIDGE", "1") != "0" and not agent_presence.is_paused():
                try:
                    with _BRIDGE_PROC_LOCK:
                        alive_pid = _reap_bridge_children_locked() or _read_bridge_pid()
                    if alive_pid is None:
                        ensure_bridge_running(clear_paused=False)
                except Exception:
                    pass
            stop_supervisor.wait(3.0)

    if os.environ.get("WASH_AUTO_BRIDGE", "1") != "0" and not agent_presence.is_paused():
        try:
            ensure_bridge_running(clear_paused=False)
        except Exception:
            pass
    supervisor = threading.Thread(target=_supervisor_loop, name="bridge-supervisor", daemon=True)
    supervisor.start()
    if os.environ.get("WASH_AUTO_CHECK_UPDATE", "1") != "0":
        def _boot_update_check() -> None:
            time.sleep(1.5)
            try:
                update_manager.get_status(force_refresh=True)
            except Exception:
                pass
        threading.Thread(target=_boot_update_check, name="boot-update-check", daemon=True).start()
    try:
        yield
    finally:
        stop_supervisor.set()


app = FastAPI(title="SHZ 洗片控制台", version="1.0.0", lifespan=_app_lifespan)


class ResourceLockManager:
    """TTL-guarded in-process mutex manager for serializing brief shared UI/layout steps."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._locks: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _clean_name(lock_name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (lock_name or "").strip()).strip("_")[:80]
        if not cleaned:
            raise ValueError("锁名称不能为空")
        return cleaned

    def _evict_expired_locked(self, now: float) -> None:
        expired = [k for k, v in self._locks.items() if now >= float(v["expiresAt"])]
        for k in expired:
            self._locks.pop(k, None)

    def acquire(
        self,
        lock_name: str,
        owner: str = "worker",
        ttl_sec: float = 90.0,
        wait_sec: float = 30.0,
    ) -> dict[str, Any]:
        key = self._clean_name(lock_name)
        clean_owner = (owner or "worker").strip()[:80] or "worker"
        ttl = max(5.0, min(float(ttl_sec or 90.0), 300.0))
        wait = max(0.0, min(float(wait_sec or 30.0), 60.0))
        deadline = time.monotonic() + wait

        with self._cv:
            while True:
                now = time.monotonic()
                self._evict_expired_locked(now)
                existing = self._locks.get(key)
                if existing is None:
                    token = uuid.uuid4().hex[:12]
                    self._locks[key] = {
                        "owner": clean_owner,
                        "token": token,
                        "expiresAt": now + ttl,
                        "acquiredAt": time.time(),
                    }
                    return {
                        "ok": True,
                        "acquired": True,
                        "lock": key,
                        "owner": clean_owner,
                        "token": token,
                        "ttlSec": round(ttl, 2),
                    }
                remaining = deadline - now
                if remaining <= 0:
                    return {
                        "ok": False,
                        "acquired": False,
                        "lock": key,
                        "heldBy": existing["owner"],
                        "expiresInSec": round(max(0.0, float(existing["expiresAt"]) - now), 2),
                    }
                until_expire = max(0.05, float(existing["expiresAt"]) - now)
                self._cv.wait(timeout=min(remaining, until_expire))

    def release(self, lock_name: str, token: str = "") -> dict[str, Any]:
        key = self._clean_name(lock_name)
        clean_token = (token or "").strip()
        with self._cv:
            now = time.monotonic()
            self._evict_expired_locked(now)
            existing = self._locks.get(key)
            if existing is None:
                return {"ok": True, "released": False, "lock": key, "reason": "not_held"}
            if clean_token and existing["token"] != clean_token:
                return {
                    "ok": False,
                    "released": False,
                    "conflict": True,
                    "lock": key,
                    "heldBy": existing["owner"],
                }
            self._locks.pop(key, None)
            self._cv.notify_all()
            return {"ok": True, "released": True, "lock": key}

    def release_by_owner(self, lock_name: str, owner: str) -> dict[str, Any]:
        """Release `lock_name` only if currently held by `owner` (used on abort/stall cleanup)."""
        key = self._clean_name(lock_name)
        clean_owner = (owner or "").strip()[:80]
        if not clean_owner:
            return {"ok": True, "released": False, "lock": key, "reason": "empty_owner"}
        with self._cv:
            now = time.monotonic()
            self._evict_expired_locked(now)
            existing = self._locks.get(key)
            if existing is None or existing.get("owner") != clean_owner:
                return {"ok": True, "released": False, "lock": key, "reason": "not_held_by_owner"}
            self._locks.pop(key, None)
            self._cv.notify_all()
            return {"ok": True, "released": True, "lock": key, "owner": clean_owner}

    def list_active(self) -> list[dict[str, Any]]:
        with self._cv:
            now = time.monotonic()
            self._evict_expired_locked(now)
            return [
                {
                    "lock": k,
                    "owner": v["owner"],
                    "expiresInSec": round(max(0.0, float(v["expiresAt"]) - now), 2),
                }
                for k, v in self._locks.items()
            ]


resource_locks = ResourceLockManager()
store = QueueStore()

STATIC_DIR = APP_ROOT / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_PROJECT_CACHES: dict[str, dict[str, Any]] = {
    "libtv": {"fetchedAt": 0.0, "projects": []},
    "jimeng": {"fetchedAt": 0.0, "projects": []},
}


def _get_projects_cached(
    platform: str = "libtv",
    force: bool = False,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    plat = normalize_platform(platform)
    cache = _PROJECT_CACHES[plat]
    now = time.monotonic()
    if not force and cache["projects"] and (now - float(cache["fetchedAt"]) < 60.0):
        return cache["projects"], None, False
    try:
        if plat == "jimeng":
            projects = jimeng_bridge.list_projects()
        else:
            raw_list = libtv_bridge.list_projects()
            projects = [{**p, "platform": "libtv"} for p in raw_list]
        if projects or plat == "jimeng":
            cache["projects"] = projects
            cache["fetchedAt"] = now
        return cache["projects"], None, False
    except (jimeng_bridge.JimengAuthRequiredError, libtv_bridge.LibtvAuthRequiredError) as exc:
        return cache["projects"], str(exc), True
    except Exception as exc:
        return cache["projects"], str(exc), False

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("image/webp", ".webp")


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _iter_file_range(path: Path, start: int, end: int, chunk_size: int = 262144) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _stream_media_file(request: Request, target: Path, *, download_name: str = "") -> Response:
    size = target.stat().st_size
    mime = _guess_mime(target)
    range_header = request.headers.get("range")
    base_headers: dict[str, str] = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    clean_dl = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", Path(download_name or "").name).strip()
    if clean_dl:
        ascii_fallback = re.sub(r"[^\x20-\x7e]+", "_", clean_dl).strip("._") or target.name
        base_headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quote(clean_dl)}'
        )

    if not range_header or size == 0:
        return FileResponse(target, media_type=mime, headers=base_headers)

    match = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
    if not match:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    start_str, end_str = match.groups()
    if not start_str and not end_str:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    if start_str == "":
        suffix = int(end_str)
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_str)
        end = int(end_str) if end_str else size - 1

    if start >= size or end < start:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)

    headers = {
        **base_headers,
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(
        _iter_file_range(target, start, end),
        status_code=206,
        media_type=mime,
        headers=headers,
    )


class PlatformSwitchReq(BaseModel):
    platform: str


class DefaultProjectReq(BaseModel):
    projectUuid: str
    projectName: str = ""
    workspaceId: str | int | None = None
    platform: str = ""
    canvasUrl: str = ""


class ProjectCreateReq(BaseModel):
    name: str
    platform: str = ""
    workspaceId: str | int | None = "0"
    createWorkspace: bool = False
    description: str = ""
    setAsDefault: bool = True


class ShotProjectReq(BaseModel):
    projectUuid: str = ""
    projectName: str = ""
    canvasUrl: str = ""
    workspaceId: str | int | None = None
    platform: str | None = None
    setAsDefault: bool = True


class PromptUpdateReq(BaseModel):
    promptText: str
    note: str = ""


class PresetPatchReq(BaseModel):
    name: str | None = None
    scene: str | None = None
    roleHint: str | None = None
    libtvNodeKey: str | None = None
    projectUuid: str | None = None


class ReprocessReq(BaseModel):
    reason: str = ""
    edited_prompt: str | None = None


class RunShotReq(BaseModel):
    edited_prompt: str | None = None
    targetDuration: int | str | None = None


class MarkPassedReq(BaseModel):
    versionId: str = ""
    sha256: str = ""
    role: str = ""
    mediaUrl: str = ""
    syncLocal: bool = True


class RollbackReq(BaseModel):
    versionId: str
    syncToCanvas: bool = True


class ShotSceneReq(BaseModel):
    scene: str = ""


class SceneCreateReq(BaseModel):
    name: str = ""


class SceneRenameReq(BaseModel):
    oldName: str = ""
    newName: str = ""


class ShotMetaReq(BaseModel):
    title: str | None = None
    targetDuration: int | str | None = None
    syncLocal: bool = True


class ExportConfigReq(BaseModel):
    defaultExportDir: str | None = None
    sceneExportDirs: dict[str, str] | None = None
    autoSyncExport: bool | None = None


class ShotExportLocalReq(BaseModel):
    role: str = "1080p"
    exportDir: str = ""
    title: str | None = None


class BatchExportReq(BaseModel):
    scene: str = ""
    role: str = "1080p"


class PickFolderReq(BaseModel):
    initialDir: str = ""
    prompt: str = "选择成片同步导出文件夹"


class ErrorRecoverReq(BaseModel):
    mode: str = "retry"


class JimengCreditCeilingReq(BaseModel):
    creditCeiling: int = 0


class AutoQaConfigReq(BaseModel):
    autoQaAfterRender: bool = False


class AgentUpdateReq(BaseModel):
    status: str | None = None
    platform: str | None = None
    projectUuid: str | None = None
    projectName: str | None = None
    canvasUrl: str | None = None
    promptText: str | None = None
    promptFilePath: str | None = None
    libtvGroupKey: str | None = None
    libtvSampleNodeKey: str | None = None
    libtvFinalNodeKey: str | None = None
    sourceLibtvNodeKey: str | None = None
    depthLibtvNodeKey: str | None = None
    compLibtvNodeKey: str | None = None
    charLibtvNodeKeys: dict[str, str] | None = None
    libtvMixedOrder: list[dict[str, Any]] | None = None
    jimengMeta: dict[str, Any] | None = None
    agentSummary: str | None = None
    errorReason: str | None = None
    errorSolution: str | None = None
    errorDetail: str | None = None
    whiteoutUpdates: list[dict[str, Any]] | None = None
    confirmationRequest: dict[str, Any] | None = None
    draftVideo: dict[str, Any] | None = None
    finalVideo: dict[str, Any] | None = None
    resolveFeedbacks: bool = False
    revisionSummary: str | None = None
    revisionDirection: str | None = None
    conversationId: str | None = None
    desktopConversationId: str | None = None
    claimToken: str | None = None


class ClaimReq(BaseModel):
    workerId: str = "coordinator"
    limit: int = 10
    shotIds: list[str] | None = None
    fastpathOnly: bool = False
    aiOnly: bool = False


class CoordinatorClaimReq(BaseModel):
    workerId: str = "desktop_coordinator"
    limit: int = 2
    shotIds: list[str] | None = None


class AiDispatchModeReq(BaseModel):
    mode: str = "desktop_subagent"


class ReleaseFastpathReq(BaseModel):
    claimToken: str = ""
    reason: str = ""
    nextStatus: str = ""


class BindConvReq(BaseModel):
    conversationId: str
    surface: str = "desktop"


class ClaimShotReq(BaseModel):
    workerId: str = "worker"


class LockAcquireReq(BaseModel):
    owner: str = "worker"
    ttlSec: float = 90.0
    waitSec: float = 30.0


class LockReleaseReq(BaseModel):
    token: str = ""


class LockOwnerReleaseReq(BaseModel):
    owner: str = ""


class HeartbeatReq(BaseModel):
    watcherId: str = "agy_bridge"
    mode: str = "bridge"
    busy: bool = False
    activeShotIds: list[str] | None = None
    pid: int | None = None
    ttlSec: float = 40.0
    lastConversationId: str | None = None
    lastRunAt: str | None = None
    lastError: str | None = None
    shotWorkers: dict[str, dict[str, Any]] | None = None
    watcherToken: str = ""
    replaceToken: bool = False


class RetryProcessingReq(BaseModel):
    mode: str = "retry"
    reason: str = ""
    actor: str = "user"


class OpenShotChatReq(BaseModel):
    mode: str = "auto"
    note: str = ""
    dryRun: bool = False


class OpenAppReq(BaseModel):
    target: str = "antigravity"
    dryRun: bool = False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": update_manager.current_version(),
        "workspaceRoot": str(WORKSPACE_ROOT),
        "dataRoot": str(DATA_ROOT),
    }


@app.get("/api/system/update-status")
def system_update_status(refresh: bool = Query(default=False)) -> dict[str, Any]:
    return update_manager.get_status(force_refresh=bool(refresh))


@app.post("/api/system/check-update")
def system_check_update() -> dict[str, Any]:
    return update_manager.get_status(force_refresh=True)


@app.post("/api/system/apply-update")
def system_apply_update() -> dict[str, Any]:
    port = int(os.environ.get("WASH_APP_PORT", "8787"))
    try:
        return update_manager.apply_update(workspace_root=WORKSPACE_ROOT, port=port, auto_restart=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/system/rollback-update")
def system_rollback_update() -> dict[str, Any]:
    port = int(os.environ.get("WASH_APP_PORT", "8787"))
    try:
        return update_manager.rollback_update(workspace_root=WORKSPACE_ROOT, port=port, auto_restart=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/system/open-antigravity")
def open_antigravity_window(req: OpenAppReq | None = None) -> dict[str, Any]:
    body = req or OpenAppReq()
    try:
        res = shot_fastpath.launch_foreground_app_or_terminal(
            target=body.target,
            workspace_root=WORKSPACE_ROOT,
            agy_bin=agent_bridge.find_agy_bin(),
            dry_run=bool(body.dryRun),
        )
        return {"ok": True, **res}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/system/pick-folder")
def pick_local_folder(req: PickFolderReq | None = None) -> dict[str, Any]:
    body = req or PickFolderReq()
    if sys.platform != "darwin":
        return {"ok": False, "unsupported": True, "path": "", "detail": "非 macOS 环境请直接输入文件夹绝对路径"}
    script = "\n".join(
        [
            "on run argv",
            "  set promptText to item 1 of argv",
            '  if (count of argv) >= 2 and (item 2 of argv) is not "" then',
            "    try",
            "      set startPath to POSIX file (item 2 of argv)",
            "      return POSIX path of (choose folder with prompt promptText default location startPath)",
            "    on error",
            "      return POSIX path of (choose folder with prompt promptText)",
            "    end try",
            "  else",
            "    return POSIX path of (choose folder with prompt promptText)",
            "  end if",
            "end run",
        ]
    )
    args = ["/usr/bin/osascript", "-", body.prompt or "选择成片同步导出文件夹"]
    init_dir = str(body.initialDir or "").strip()
    if init_dir and Path(init_dir).expanduser().is_dir():
        args.append(str(Path(init_dir).expanduser().resolve()))
    try:
        proc = subprocess.run(args, input=script, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=408, detail="选择文件夹超时") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "(-128)" in err or "User canceled" in err:
            return {"ok": False, "cancelled": True, "path": ""}
        raise HTTPException(status_code=400, detail=err or "打开 Finder 选择文件夹失败")
    chosen = proc.stdout.strip().rstrip("/") or "/"
    return {"ok": True, "cancelled": False, "path": chosen}


@app.post("/api/config/export-dirs")
def update_export_dirs(req: ExportConfigReq) -> dict[str, Any]:
    try:
        cfg = store.set_export_config(
            default_export_dir=req.defaultExportDir,
            scene_export_dirs=req.sceneExportDirs,
            auto_sync_export=req.autoSyncExport,
        )
        return {"ok": True, **cfg}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/exports/sync-all")
def sync_all_local_exports(req: BatchExportReq | None = None) -> dict[str, Any]:
    body = req or BatchExportReq()
    try:
        res = store.sync_all_local_exports(scene=body.scene, role=body.role)
        return {"ok": True, **res}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/media/{rel_path:path}")
def serve_media(rel_path: str, request: Request, download_name: str = Query(default="")) -> Response:
    target = (DATA_ROOT / rel_path).resolve()
    root = DATA_ROOT.resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    return _stream_media_file(request, target, download_name=download_name)


@app.get("/api/state")
def get_state(refresh_projects: bool = Query(default=False)) -> dict[str, Any]:
    state = store.get_state()
    default_plat = normalize_platform(state.get("defaultPlatform"))
    pdefs = state.get("platformDefaults") or {}

    libtv_status = libtv_bridge.get_auth_status(force=refresh_projects)
    jimeng_status = jimeng_bridge.get_auth_status(force=refresh_projects)

    libtv_projects: list[dict[str, Any]] = list(_PROJECT_CACHES["libtv"]["projects"])
    libtv_err: str | None = None
    libtv_auth_req = not bool(libtv_status.get("authenticated"))
    if libtv_status.get("authenticated"):
        libtv_projects, libtv_err, libtv_auth_req = _get_projects_cached(
            "libtv", force=refresh_projects or not libtv_projects
        )
        if libtv_auth_req:
            libtv_status = libtv_bridge.get_auth_status(force=True)
        elif not (pdefs.get("libtv") or {}).get("projectUuid") and libtv_projects:
            first_lt = libtv_projects[0]
            updated_q = store.set_default_project(
                first_lt["uuid"],
                first_lt.get("name") or first_lt["uuid"],
                workspace_id=first_lt.get("workspaceId") if first_lt.get("workspaceId") is not None else "",
                platform="libtv",
                canvas_url=first_lt.get("url") or "",
            )
            state["platformDefaults"] = updated_q.get("platformDefaults", pdefs)
            if default_plat == "libtv":
                state["defaultProjectUuid"] = updated_q.get("defaultProjectUuid", "")
                state["defaultProjectName"] = updated_q.get("defaultProjectName", "")
                state["defaultWorkspaceId"] = updated_q.get("defaultWorkspaceId", "")
    elif default_plat == "libtv":
        libtv_err = str(libtv_status.get("detail") or "LibTV 尚未登录")

    if not libtv_projects:
        ldef = state.get("platformDefaults", pdefs).get("libtv") or {}
        if ldef.get("projectUuid"):
            libtv_projects = [
                {
                    "uuid": ldef["projectUuid"],
                    "name": ldef.get("projectName") or ldef["projectUuid"],
                    "url": ldef.get("canvasUrl") or build_canvas_url(ldef["projectUuid"], platform="libtv"),
                    "platform": "libtv",
                }
            ]

    jimeng_projects: list[dict[str, Any]] = list(_PROJECT_CACHES["jimeng"]["projects"])
    jimeng_err: str | None = None
    jimeng_auth_req = not bool(jimeng_status.get("authenticated"))
    if default_plat == "jimeng" or refresh_projects or jimeng_status.get("authenticated"):
        if jimeng_status.get("authenticated"):
            jimeng_projects, jimeng_err, jimeng_auth_req = _get_projects_cached(
                "jimeng", force=refresh_projects or not jimeng_projects
            )
            if jimeng_auth_req:
                jimeng_status = jimeng_bridge.get_auth_status(force=True)
        elif default_plat == "jimeng":
            jimeng_err = str(jimeng_status.get("detail") or "即梦官网尚未登录授权")

    active_projects = jimeng_projects if default_plat == "jimeng" else libtv_projects
    state["projects"] = active_projects
    state["projectsByPlatform"] = {
        "libtv": libtv_projects,
        "jimeng": jimeng_projects,
    }
    state["projectsError"] = jimeng_err if default_plat == "jimeng" else libtv_err
    state["projectsAuthRequired"] = jimeng_auth_req if default_plat == "jimeng" else libtv_auth_req
    state["libtvStatus"] = libtv_status
    state["jimengStatus"] = jimeng_status
    state["agentStatus"] = agent_presence.snapshot(state)
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


@app.get("/api/projects")
def get_projects(platform: str = Query(default=""), refresh: bool = Query(default=True)) -> dict[str, Any]:
    plat = normalize_platform(platform or store.get_state().get("defaultPlatform"))
    projects, err, auth_required = _get_projects_cached(plat, force=refresh)
    if auth_required:
        return {"platform": plat, "projects": [], "error": err, "authRequired": True}
    if err and not projects:
        raise HTTPException(status_code=502, detail=f"获取 {get_platform_label(plat)} 画布列表失败: {err}")
    return {"platform": plat, "projects": projects, "error": err, "authRequired": False}


@app.post("/api/projects")
def create_project(req: ProjectCreateReq) -> dict[str, Any]:
    plat = normalize_platform(req.platform or store.get_state().get("defaultPlatform"))
    try:
        if plat == "jimeng":
            proj = jimeng_bridge.create_project(req.name, description=req.description)
        else:
            proj = libtv_bridge.create_project(
                req.name,
                workspace_id=req.workspaceId,
                create_workspace=req.createWorkspace,
                description=req.description,
            )
            proj = {**proj, "platform": "libtv"}
    except (jimeng_bridge.JimengAuthRequiredError, libtv_bridge.LibtvAuthRequiredError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"新建 {get_platform_label(plat)} 画布失败: {exc}") from exc

    cache = _PROJECT_CACHES[plat]
    existing = [p for p in cache.get("projects", []) if p.get("uuid") != proj.get("uuid")]
    cache["projects"] = [proj, *existing]
    cache["fetchedAt"] = time.monotonic()

    if req.setAsDefault:
        store.set_default_project(
            proj["uuid"],
            proj["name"],
            workspace_id=proj.get("workspaceId") if proj.get("workspaceId") is not None else "",
            platform=plat,
            canvas_url=proj.get("url") or "",
        )
    return {"ok": True, "platform": plat, "project": proj}


@app.post("/api/config/platform")
def set_default_platform(req: PlatformSwitchReq) -> dict[str, Any]:
    try:
        updated = store.set_default_platform(req.platform)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    plat = normalize_platform(updated.get("defaultPlatform"))
    libtv_status = libtv_bridge.get_auth_status(force=False)
    jimeng_status = jimeng_bridge.get_auth_status(force=False)
    auth_required = False
    proj_err: str | None = None
    if plat == "jimeng":
        if jimeng_status.get("authenticated"):
            projects, proj_err, auth_required = _get_projects_cached(
                "jimeng", force=not bool(_PROJECT_CACHES["jimeng"]["projects"])
            )
        else:
            projects = list(_PROJECT_CACHES["jimeng"]["projects"])
            auth_required = True
            proj_err = str(jimeng_status.get("detail") or "即梦官网尚未登录授权")
    else:
        if libtv_status.get("authenticated"):
            projects, proj_err, auth_required = _get_projects_cached(
                "libtv", force=not bool(_PROJECT_CACHES["libtv"]["projects"])
            )
        else:
            projects = list(_PROJECT_CACHES["libtv"]["projects"])
            auth_required = True
            proj_err = str(libtv_status.get("detail") or "LibTV 尚未登录")

    return {
        "ok": True,
        "defaultPlatform": plat,
        "defaultProjectUuid": updated.get("defaultProjectUuid", ""),
        "defaultProjectName": updated.get("defaultProjectName", ""),
        "defaultWorkspaceId": updated.get("defaultWorkspaceId", ""),
        "platformDefaults": updated.get("platformDefaults", {}),
        "projects": projects,
        "projectsError": proj_err,
        "projectsAuthRequired": auth_required,
        "libtvStatus": libtv_status,
        "jimengStatus": jimeng_status,
    }


@app.get("/api/libtv/auth/status")
def get_libtv_auth_status() -> dict[str, Any]:
    st = libtv_bridge.get_auth_status(force=True)
    projects: list[dict[str, Any]] = []
    if st.get("authenticated"):
        projects, _, _ = _get_projects_cached("libtv", force=not bool(_PROJECT_CACHES["libtv"]["projects"]))
    return {"ok": True, "libtvStatus": st, "projects": projects}


@app.post("/api/libtv/auth/login")
def start_libtv_auth_login() -> dict[str, Any]:
    try:
        res = libtv_bridge.start_auth_login()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"启动 LibTV 登录失败: {exc}") from exc

    projects: list[dict[str, Any]] = []
    if res.get("authenticated"):
        projects, _, _ = _get_projects_cached("libtv", force=True)
    st = libtv_bridge.get_auth_status(force=True)
    return {"ok": True, **res, "libtvStatus": st, "projects": projects}


@app.get("/api/jimeng/auth/status")
def get_jimeng_auth_status() -> dict[str, Any]:
    st = jimeng_bridge.get_auth_status(force=True)
    projects: list[dict[str, Any]] = []
    if st.get("authenticated"):
        projects, _, _ = _get_projects_cached("jimeng", force=not bool(_PROJECT_CACHES["jimeng"]["projects"]))
    return {"ok": True, "jimengStatus": st, "projects": projects}


@app.post("/api/jimeng/auth/login")
def start_jimeng_auth_login() -> dict[str, Any]:
    try:
        res = jimeng_bridge.start_auth_login()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"启动即梦登录失败: {exc}") from exc

    projects: list[dict[str, Any]] = []
    if res.get("authenticated"):
        projects, _, _ = _get_projects_cached("jimeng", force=True)
    st = jimeng_bridge.get_auth_status(force=True)
    return {"ok": True, **res, "jimengStatus": st, "projects": projects}


@app.post("/api/config/jimeng-credit-ceiling")
def set_jimeng_credit_ceiling(req: JimengCreditCeilingReq) -> dict[str, Any]:
    updated = store.set_jimeng_credit_ceiling(req.creditCeiling)
    return {"ok": True, "jimengCreditCeiling": int(updated.get("jimengCreditCeiling", 0))}


@app.post("/api/config/auto-qa")
def set_auto_qa_config(req: AutoQaConfigReq) -> dict[str, Any]:
    updated = store.set_auto_qa_after_render(req.autoQaAfterRender)
    return {"ok": True, "autoQaAfterRender": bool(updated.get("autoQaAfterRender", False))}


@app.post("/api/config/ai-dispatch-mode")
def set_ai_dispatch_mode_config(req: AiDispatchModeReq) -> dict[str, Any]:
    try:
        updated = store.set_ai_dispatch_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated.get("aiDispatchMode") == "cli_bridge":
        ensure_bridge_running(clear_paused=True)
    snap = agent_presence.snapshot(store.get_state())
    return {
        "ok": True,
        "aiDispatchMode": updated.get("aiDispatchMode", "desktop_subagent"),
        "agentStatus": snap,
    }


@app.post("/api/config/default-project")
def set_default_project(req: DefaultProjectReq) -> dict[str, Any]:
    try:
        updated = store.set_default_project(
            req.projectUuid,
            req.projectName,
            workspace_id=req.workspaceId,
            platform=req.platform or None,
            canvas_url=req.canvasUrl,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "defaultPlatform": updated.get("defaultPlatform", "libtv"),
        "defaultProjectUuid": updated["defaultProjectUuid"],
        "defaultProjectName": updated["defaultProjectName"],
        "defaultWorkspaceId": updated.get("defaultWorkspaceId", ""),
        "platformDefaults": updated.get("platformDefaults", {}),
    }


@app.get("/api/presets")
def get_presets() -> dict[str, Any]:
    return {"presets": store.list_presets()}


@app.post("/api/presets")
async def create_preset(
    name: str = Form(...),
    scene: str = Form(default="通用"),
    role_hint: str = Form(default=""),
    libtv_node_key: str = Form(default=""),
    project_uuid: str = Form(default=""),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    state = store.get_state()
    p_uuid = project_uuid.strip() or state["defaultProjectUuid"]
    preset = store.add_preset(
        name=name,
        role_hint=role_hint,
        filename=file.filename or f"{name}.jpg",
        raw_bytes=raw,
        scene=scene,
        libtv_node_key=libtv_node_key,
        project_uuid=p_uuid,
    )
    return {"ok": True, "preset": preset}


@app.patch("/api/presets/{preset_id}")
def patch_preset(preset_id: str, req: PresetPatchReq) -> dict[str, Any]:
    try:
        preset = store.update_preset(preset_id, req.model_dump(exclude_none=True))
        return {"ok": True, "preset": preset}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/presets/{preset_id}")
def remove_preset(preset_id: str) -> dict[str, Any]:
    try:
        store.delete_preset(preset_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/preflight")
async def preflight_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Quick temporary preflight using the shared skill preflight_check.py."""
    import tempfile

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    suffix = Path(file.filename or "asset").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(raw)
        tmp.flush()
        result = run_preflight(Path(tmp.name))
        result["filename"] = file.filename
        return result


@app.post("/api/shots")
async def create_shot(
    shot_id: str = Form(...),
    scene: str = Form(default=""),
    title: str = Form(default=""),
    platform: str = Form(default=""),
    project_uuid: str = Form(default=""),
    project_name: str = Form(default=""),
    canvas_url: str = Form(default=""),
    notes: str = Form(default=""),
    user_prompt: str = Form(default=""),
    char_specs_json: str = Form(default="[]"),
    ordered_image_specs_json: str = Form(default=""),
    whiteout_parts_json: str = Form(default="[]"),
    whiteout_target: str = Form(default=""),
    work_mode: str = Form(default="video_wash"),
    target_duration: str = Form(default=""),
    auto_confirm: str = Form(default="true"),
    overwrite: str = Form(default="false"),
    source_video: UploadFile | None = File(default=None),
    comp_image: UploadFile | None = File(default=None),
    char_images: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    try:
        char_specs = json.loads(char_specs_json or "[]")
        if not isinstance(char_specs, list):
            raise ValueError("char_specs_json 必须是数组")
        ordered_image_specs = json.loads(ordered_image_specs_json) if ordered_image_specs_json.strip() else None
        if ordered_image_specs is not None and not isinstance(ordered_image_specs, list):
            raise ValueError("ordered_image_specs_json 必须是数组")
        whiteout_parts = json.loads(whiteout_parts_json or "[]")
        if not isinstance(whiteout_parts, list):
            raise ValueError("whiteout_parts_json 必须是数组")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"素材列表格式错误: {exc}") from exc

    src_bytes = await source_video.read() if source_video is not None else b""
    comp_bytes = await comp_image.read() if comp_image is not None else b""

    uploads: list[tuple[str, bytes]] = []
    for up in char_images:
        data = await up.read()
        uploads.append((up.filename or "asset.jpg", data))

    try:
        shot = store.create_or_replace_shot(
            shot_id=shot_id,
            scene=scene,
            title=title,
            platform=platform.strip(),
            work_mode=work_mode.strip(),
            target_duration=target_duration.strip(),
            project_uuid=project_uuid.strip(),
            project_name=project_name.strip(),
            canvas_url=canvas_url.strip(),
            notes=notes,
            user_prompt=user_prompt,
            source_filename=sanitize_filename(source_video.filename or "source.mp4") if source_video else "",
            source_bytes=src_bytes,
            comp_filename=sanitize_filename(comp_image.filename or "comp.jpg") if comp_image else "",
            comp_bytes=comp_bytes,
            char_specs=char_specs,
            char_uploads=uploads,
            ordered_image_specs=ordered_image_specs,
            whiteout_parts=[str(x).strip() for x in whiteout_parts if str(x).strip()],
            whiteout_target=whiteout_target.strip(),
            auto_confirm=(auto_confirm.lower() != "false"),
            overwrite=(overwrite.lower() == "true"),
        )
        return {"ok": True, "shot": shot}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/confirm")
def confirm_shot(shot_id: str) -> dict[str, Any]:
    try:
        shot = store.confirm_shot(shot_id)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/feedback")
async def submit_feedback(
    shot_id: str,
    comment: str = Form(default=""),
    tags_json: str = Form(default="[]"),
    target_slot: str = Form(default=""),
    edited_prompt: str = Form(default=""),
    target_duration: str = Form(default=""),
    update_target_duration: str = Form(default=""),
    attachments: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    try:
        tags = json.loads(tags_json or "[]")
    except Exception:
        tags = []

    saved_attachments: list[tuple[str, bytes]] = []
    for up in attachments:
        data = await up.read()
        if data:
            saved_attachments.append((up.filename or "attachment.png", data))

    upd_dur = update_target_duration.strip().lower() in {"1", "true", "yes", "on"}
    if not comment.strip() and not tags and not saved_attachments and not edited_prompt.strip() and not upd_dur:
        raise HTTPException(status_code=400, detail="请至少修改提示词、调整时长、填写修改意见、勾选标签或附带图片")

    try:
        shot = store.submit_feedback(
            shot_id,
            comment,
            tags,
            target_slot,
            saved_attachments,
            edited_prompt,
            target_duration=target_duration.strip(),
            update_target_duration=upd_dur,
        )
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/mask-image")
async def save_manual_mask(
    shot_id: str,
    slot_tag: str = Form(default=""),
    slot_id: str = Form(default=""),
    sync_to_canvas: str = Form(default="false"),
    is_reset: str = Form(default="false"),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="涂白图像文件为空")
    try:
        shot = store.apply_manual_mask(
            shot_id,
            slot_tag=slot_tag,
            slot_id=slot_id,
            filename=sanitize_filename(file.filename or "mask.png", "mask.png"),
            raw_bytes=raw,
            sync_to_canvas=(sync_to_canvas.lower() == "true"),
            is_reset=(is_reset.lower() == "true"),
        )
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/resolve-confirmation")
async def resolve_confirmation(
    shot_id: str,
    decision: str = Form(default="approve"),
    comment: str = Form(default=""),
    corrected_mask: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    corrected: tuple[str, bytes] | None = None
    if corrected_mask is not None:
        raw = await corrected_mask.read()
        if raw:
            corrected = (corrected_mask.filename or "corrected_mask.png", raw)
    try:
        shot = store.resolve_confirmation(shot_id, decision, comment, corrected)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/run")
def run_shot(shot_id: str, req: RunShotReq | None = None) -> dict[str, Any]:
    body = req or RunShotReq()
    try:
        shot = store.request_run(
            shot_id,
            edited_prompt=body.edited_prompt,
            target_duration=body.targetDuration,
            update_target_duration=("targetDuration" in body.model_fields_set),
        )
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/approve-final")
def approve_final(shot_id: str) -> dict[str, Any]:
    try:
        shot = store.request_final(shot_id)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/mark-passed")
def mark_version_passed(shot_id: str, req: MarkPassedReq | None = None) -> dict[str, Any]:
    body = req or MarkPassedReq()
    try:
        res = store.mark_video_version_passed(
            shot_id,
            version_id=body.versionId,
            sha256=body.sha256,
            role=body.role,
            media_url=body.mediaUrl,
            sync_local=body.syncLocal,
        )
        return {"ok": True, **res}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/reprocess")
def reprocess_shot(shot_id: str, req: ReprocessReq | None = None) -> dict[str, Any]:
    body = req or ReprocessReq()
    try:
        prev_shot = store.get_shot(shot_id)
        if prev_shot.get("status") == "processing" and prev_shot.get("claimToken"):
            agent_presence.request_cancel(prev_shot["id"], str(prev_shot.get("claimToken") or ""))
    except KeyError:
        pass
    try:
        shot = store.reprocess_shot(
            shot_id,
            reason=body.reason,
            edited_prompt=body.edited_prompt,
        )
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/recover-error")
def recover_shot_error(shot_id: str, req: ErrorRecoverReq | None = None) -> dict[str, Any]:
    body = req or ErrorRecoverReq()
    try:
        shot = store.recover_from_error(shot_id, mode=body.mode)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/open-chat")
def open_shot_foreground_chat(shot_id: str, req: OpenShotChatReq | None = None) -> dict[str, Any]:
    body = req or OpenShotChatReq()
    try:
        shot = store.get_shot(shot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    port = int(os.environ.get("WASH_APP_PORT", "8787"))
    try:
        res = shot_fastpath.launch_foreground_shot_chat(
            shot,
            mode=body.mode,
            user_note=body.note,
            port=port,
            workspace_root=WORKSPACE_ROOT,
            agy_bin=agent_bridge.find_agy_bin(),
            dry_run=bool(body.dryRun),
        )
        return {"ok": True, **res}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/handoff-context")
def get_shot_handoff_context(shot_id: str, req: OpenShotChatReq | None = None) -> dict[str, Any]:
    body = req or OpenShotChatReq()
    try:
        shot = store.get_shot(shot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    port = int(os.environ.get("WASH_APP_PORT", "8787"))
    context_text = shot_fastpath.build_shot_handoff_context(
        shot,
        workspace_root=WORKSPACE_ROOT,
        port=port,
        user_note=body.note,
    )
    return {
        "ok": True,
        "shotId": shot["id"],
        "cliConversationId": shot.get("cliConversationId", ""),
        "desktopConversationId": resolve_desktop_conversation_id(shot),
        "canResumeCliChat": bool(shot.get("canResumeCliChat")),
        "canResumeDesktopSubagent": bool(shot.get("canResumeDesktopSubagent")),
        "contextText": context_text,
    }


@app.get("/api/shots/{shot_id}")
def get_shot_detail(shot_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "shot": store.get_shot(shot_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/update-prompt")
def update_shot_prompt(shot_id: str, req: PromptUpdateReq) -> dict[str, Any]:
    try:
        shot = store.request_prompt_update(shot_id, req.promptText, req.note)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/rollback")
def rollback_shot(shot_id: str, req: RollbackReq) -> dict[str, Any]:
    try:
        shot = store.rollback_shot(shot_id, req.versionId, sync_to_canvas=req.syncToCanvas)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/shots/{shot_id}/project")
def update_shot_project(shot_id: str, req: ShotProjectReq) -> dict[str, Any]:
    try:
        shot = store.update_shot_project(
            shot_id,
            req.projectUuid,
            project_name=req.projectName,
            canvas_url=req.canvasUrl,
            workspace_id=req.workspaceId,
            platform=req.platform,
        )
        eff_plat = normalize_platform(shot.get("platform"))
        if req.setAsDefault and shot.get("projectUuid"):
            store.set_default_project(
                shot["projectUuid"],
                shot.get("projectName") or "",
                workspace_id=req.workspaceId if req.workspaceId is not None else "",
                platform=eff_plat,
                canvas_url=shot.get("canvasUrl") or "",
            )
            store.set_default_platform(eff_plat)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/shots/{shot_id}/scene")
def update_shot_scene(shot_id: str, req: ShotSceneReq) -> dict[str, Any]:
    try:
        shot = store.update_shot_scene(shot_id, req.scene)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/shots/{shot_id}/meta")
def update_shot_meta(shot_id: str, req: ShotMetaReq) -> dict[str, Any]:
    try:
        res = store.update_shot_meta(
            shot_id,
            title=req.title,
            target_duration=req.targetDuration,
            update_target_duration=("targetDuration" in req.model_fields_set),
            sync_local=bool(req.syncLocal),
        )
        return {"ok": True, **res}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/export-local")
def export_shot_local(shot_id: str, req: ShotExportLocalReq | None = None) -> dict[str, Any]:
    body = req or ShotExportLocalReq()
    try:
        res = store.sync_shot_local_export(
            shot_id,
            role=body.role,
            export_dir=body.exportDir,
            title=body.title,
        )
        return {"ok": True, **res}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/replace-final")
async def replace_shot_final(
    shot_id: str,
    file: UploadFile = File(...),
    syncLocal: str = Form(default="true"),
) -> dict[str, Any]:
    raw = await file.read()
    try:
        res = store.replace_shot_final_video(
            shot_id,
            filename=sanitize_filename(file.filename or "final_1080p.mp4", "final_1080p.mp4"),
            raw_bytes=raw,
            sync_local=(syncLocal.lower() != "false"),
        )
        return {"ok": True, **res}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/scenes")
def create_scene(req: SceneCreateReq) -> dict[str, Any]:
    try:
        result = store.create_scene(req.name)
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/scenes/rename")
def rename_scene(req: SceneRenameReq) -> dict[str, Any]:
    try:
        result = store.rename_scene(req.oldName, req.newName)
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/scenes")
def delete_scene(name: str = Query(...)) -> dict[str, Any]:
    try:
        result = store.delete_scene(name)
        return {"ok": True, **result}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_jimeng_default_project(state_data: dict[str, Any] | None = None) -> str:
    st = state_data or store.get_state()
    pdefs = st.get("platformDefaults") if isinstance(st.get("platformDefaults"), dict) else {}
    jm_def = pdefs.get("jimeng") if isinstance(pdefs.get("jimeng"), dict) else {}
    return str(jm_def.get("defaultProjectUuid") or st.get("defaultProjectUuid") or "").strip()


def _sync_one_shot_with_canvas(
    shot: dict[str, Any],
    *,
    fallback_project_uuid: str = "",
    prefetched_videos_by_proj: dict[str, list[dict[str, Any]]] | None = None,
    prefetched_nodes_by_proj: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    plat = normalize_platform(shot.get("platform"))
    shot_dir = SHOTS_DIR / shot["id"]
    if plat == "jimeng":
        info = jimeng_bridge.sync_shot_node(
            shot,
            shot_dir,
            fallback_project_uuid=fallback_project_uuid,
            prefetched_videos_by_proj=prefetched_videos_by_proj,
            prefetched_nodes_by_proj=prefetched_nodes_by_proj,
        )
    elif (
        str(shot.get("status") or "") in {"final_ready", "final_done"}
        and not shot.get("finalModifiedReopened")
        and isinstance(shot.get("passedVersion"), dict)
    ):
        updated = store.get_shot(shot["id"])
        updated["_syncDelta"] = {"changed": False, "draftUpdated": False, "finalUpdated": False, "metadataUpdated": False}
        return updated, {}, plat
    else:
        info = libtv_bridge.sync_shot_node(shot, shot_dir)

    updated = store.update_shot_from_libtv_sync(
        shot["id"],
        prompt_text=info.get("promptText") or None,
        mixed_order=info.get("mixedOrder"),
        draft_path=info.get("draftPath"),
        remote_draft_url=info.get("remoteUrl") or "",
        watermark=info.get("watermark"),
        node_key=info.get("nodeKey"),
        resource_id=str(info.get("resourceId") or ""),
        draft_created_ms=int(info.get("draftCreatedAtMs") or 0),
        final_path=info.get("finalPath"),
        remote_final_url=info.get("finalRemoteUrl") or "",
        final_watermark=info.get("finalWatermark"),
        final_node_key=info.get("finalNodeKey"),
        final_resource_id=str(info.get("finalResourceId") or ""),
        final_created_ms=int(info.get("finalCreatedAtMs") or 0),
        project_uuid=info.get("projectUuid") or None,
    )
    return updated, info, plat


@app.post("/api/shots/sync-all-canvas")
def sync_all_shots_canvas() -> dict[str, Any]:
    state = store.get_state()
    shots = list(state.get("shots") or [])
    fallback_proj = _resolve_jimeng_default_project(state)

    eligible = [
        s
        for s in shots
        if str(s.get("status") or "") not in {"draft", "drafting", "processing"}
    ]
    jimeng_shots = [s for s in eligible if normalize_platform(s.get("platform")) == "jimeng"]
    prefetched_videos: dict[str, list[dict[str, Any]]] | None = None
    prefetched_nodes: dict[str, dict[str, dict[str, Any]]] | None = None
    if jimeng_shots:
        try:
            prefetched_videos, prefetched_nodes = jimeng_bridge.prefetch_canvas_sync_context(
                jimeng_shots,
                fallback_project_uuid=fallback_proj,
            )
        except jimeng_bridge.JimengAuthRequiredError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"扫描即梦画布失败: {exc}") from exc

    checked_count = 0
    updated_ids: list[str] = []
    draft_updated_ids: list[str] = []
    final_updated_ids: list[str] = []
    pre_edit_synced_ids: list[str] = []
    relinked_ids: list[str] = []
    skipped_ids: list[str] = []
    errors: list[dict[str, str]] = []

    for s in eligible:
        sid = str(s.get("id") or "")
        checked_count += 1
        try:
            updated, _info, _plat = _sync_one_shot_with_canvas(
                s,
                fallback_project_uuid=fallback_proj,
                prefetched_videos_by_proj=prefetched_videos,
                prefetched_nodes_by_proj=prefetched_nodes,
            )
            delta = updated.pop("_syncDelta", {}) if isinstance(updated, dict) else {}
            if delta.get("changed"):
                updated_ids.append(sid)
            if delta.get("draftIsPreEdit") or delta.get("finalIsPreEdit"):
                pre_edit_synced_ids.append(sid)
            if delta.get("draftUpdated") and not delta.get("draftIsPreEdit"):
                draft_updated_ids.append(sid)
            if delta.get("finalUpdated") and not delta.get("finalIsPreEdit"):
                final_updated_ids.append(sid)
            if delta.get("metadataUpdated") and not delta.get("draftUpdated") and not delta.get("finalUpdated"):
                relinked_ids.append(sid)
        except (jimeng_bridge.JimengAuthRequiredError, libtv_bridge.LibtvAuthRequiredError) as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError:
            skipped_ids.append(sid)
        except Exception as exc:
            errors.append({"shotId": sid, "error": str(exc)})

    return {
        "ok": True,
        "checkedCount": checked_count,
        "updatedCount": len(updated_ids),
        "updatedShotIds": updated_ids,
        "draftUpdatedIds": draft_updated_ids,
        "finalUpdatedIds": final_updated_ids,
        "preEditSyncedIds": pre_edit_synced_ids,
        "relinkedIds": relinked_ids,
        "skippedIds": skipped_ids,
        "errors": errors,
    }


@app.post("/api/shots/{shot_id}/sync-canvas")
@app.post("/api/shots/{shot_id}/sync-libtv")
def sync_libtv(shot_id: str) -> dict[str, Any]:
    try:
        shot = store.get_shot(shot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    plat = normalize_platform(shot.get("platform"))
    plat_label = get_platform_label(plat)
    fallback_proj = _resolve_jimeng_default_project() if plat == "jimeng" else ""
    try:
        updated, info, plat = _sync_one_shot_with_canvas(
            shot,
            fallback_project_uuid=fallback_proj,
        )
    except (jimeng_bridge.JimengAuthRequiredError, libtv_bridge.LibtvAuthRequiredError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"查询或下载 {plat_label} 节点失败: {exc}") from exc

    delta = updated.pop("_syncDelta", {}) if isinstance(updated, dict) else {}
    return {
        "ok": True,
        "platform": plat,
        "shot": updated,
        "syncDelta": delta,
        "libtv": {
            "nodeKey": info.get("nodeKey"),
            "finalNodeKey": info.get("finalNodeKey"),
            "loading": info.get("loading"),
            "taskStatus": info.get("taskStatus"),
            "hasRemoteDraft": bool(info.get("remoteUrl") or info.get("draftPath")),
            "hasRemoteFinal": bool(info.get("finalRemoteUrl") or info.get("finalPath")),
        },
    }


@app.post("/api/shots/{shot_id}/agent-update")
def agent_update(shot_id: str, req: AgentUpdateReq) -> dict[str, Any]:
    try:
        before_shot: dict[str, Any] | None = None
        try:
            before_shot = store.get_shot(shot_id)
        except KeyError:
            before_shot = None
        shot = store.agent_update_shot(shot_id, req.model_dump(exclude_unset=True))
        if (
            (before_shot and before_shot.get("claimedBy") == "desktop_coordinator")
            or shot.get("claimedBy") == "desktop_coordinator"
            or (
                store.get_ai_dispatch_mode() == "desktop_subagent"
                and (before_shot or {}).get("claimedBy") != "agy_bridge"
                and bool(shot.get("desktopConversationId") or req.desktopConversationId)
            )
        ):
            agent_presence.touch_desktop_activity(
                ttl_sec=600.0 if shot.get("status") == "processing" else 120.0
            )
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ClaimConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/claim")
def claim_shot(shot_id: str, req: ClaimShotReq | None = None) -> dict[str, Any]:
    body = req or ClaimShotReq()
    try:
        shot = store.claim_shot(shot_id, worker_id=body.workerId)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ClaimConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/shots/{shot_id}")
def delete_shot(shot_id: str) -> dict[str, Any]:
    try:
        store.delete_shot(shot_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/files/read-text")
def read_workspace_text(path: str = Query(...)) -> dict[str, Any]:
    target = Path(path).resolve()
    allowed_roots = [(WORKSPACE_ROOT / "outputs").resolve(), DATA_ROOT.resolve()]
    if not any(r in target.parents for r in allowed_roots) or not target.is_file():
        raise HTTPException(status_code=404, detail="仅支持读取 outputs 或 wash_queue_data 下的文本文件")
    return {"path": str(target), "content": target.read_text(encoding="utf-8")}


@app.get("/api/agent/pending")
def get_agent_pending() -> dict[str, Any]:
    return store.get_pending_for_agent()


@app.get("/api/agent/status")
def get_agent_status() -> dict[str, Any]:
    return agent_presence.snapshot(store.get_state())


@app.get("/api/shots/{shot_id}/agent-status")
def get_shot_agent_runtime_status(shot_id: str) -> dict[str, Any]:
    try:
        shot = store.get_shot(shot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    runtime = agent_presence.inspect_shot_runtime(shot)
    return {"ok": True, "shot": shot, "runtime": runtime}


@app.post("/api/shots/{shot_id}/retry-processing")
def retry_processing_shot(shot_id: str, req: RetryProcessingReq | None = None) -> dict[str, Any]:
    body = req or RetryProcessingReq()
    try:
        before_shot = store.get_shot(shot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    old_token = str(before_shot.get("claimToken") or "").strip()
    cancel_info = agent_presence.request_cancel(before_shot["id"], old_token)
    resource_locks.release_by_owner("chrome_ui", before_shot["id"])
    try:
        res = store.reset_stuck_shot(
            before_shot["id"],
            mode=body.mode,
            reason=body.reason,
            actor=body.actor,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    updated_shot = res["shot"]
    if (body.mode or "retry").strip().lower() != "restore" and not agent_presence.is_paused():
        ensure_bridge_running(clear_paused=False)
    runtime = agent_presence.inspect_shot_runtime(updated_shot)
    return {"ok": True, "cancelInfo": cancel_info, "runtime": runtime, **res}


@app.post("/api/agent/heartbeat")
def post_agent_heartbeat(req: HeartbeatReq) -> dict[str, Any]:
    snap = agent_presence.record_heartbeat(
        req.watcherId,
        req.mode,
        busy=req.busy,
        active_shot_ids=req.activeShotIds,
        pid=req.pid,
        ttl_sec=req.ttlSec,
        last_conversation_id=req.lastConversationId,
        last_run_at=req.lastRunAt,
        last_error=req.lastError,
        shot_workers=req.shotWorkers,
        watcher_token=req.watcherToken,
        replace_token=req.replaceToken,
    )
    if (
        str(req.mode or "").strip().lower() == "desktop"
        and req.replaceToken
        and req.watcherToken.strip()
    ):
        store.notify_waiters()
    return {"ok": True, "agentStatus": snap, "cancelTokens": agent_presence.get_active_cancel_tokens()}


@app.post("/api/agent/bridge/start")
def start_agent_bridge() -> dict[str, Any]:
    res = ensure_bridge_running(clear_paused=True)
    snap = agent_presence.snapshot(store.get_state())
    return {**res, "agentStatus": snap}


@app.post("/api/agent/bridge/stop")
def stop_agent_bridge() -> dict[str, Any]:
    res = stop_bridge_process()
    snap = agent_presence.snapshot(store.get_state())
    return {**res, "agentStatus": snap}


@app.post("/api/agent/bridge/restart")
def restart_agent_bridge() -> dict[str, Any]:
    res = restart_bridge_process()
    snap = agent_presence.snapshot(store.get_state())
    return {**res, "agentStatus": snap}


@app.post("/api/shots/{shot_id}/release-fastpath")
def release_fastpath_shot(shot_id: str, req: ReleaseFastpathReq) -> dict[str, Any]:
    try:
        shot = store.release_fastpath_claim(
            shot_id,
            claim_token=req.claimToken,
            reason=req.reason,
            next_status=req.nextStatus,
        )
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ClaimConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shots/{shot_id}/bind-conversation")
def bind_shot_conversation_endpoint(shot_id: str, req: BindConvReq) -> dict[str, Any]:
    try:
        shot = store.bind_shot_conversation(shot_id, conversation_id=req.conversationId, surface=req.surface)
        if str(req.surface or "desktop").strip().lower() != "cli":
            agent_presence.touch_desktop_activity(ttl_sec=600.0)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agent/claim")
def claim_agent_pending(req: ClaimReq | None = None) -> dict[str, Any]:
    body = req or ClaimReq()
    res = store.claim_pending(
        worker_id=body.workerId,
        limit=body.limit,
        shot_ids=body.shotIds,
        fastpath_only=bool(body.fastpathOnly),
        ai_only=bool(body.aiOnly),
    )
    return {"ok": True, **res}


@app.post("/api/agent/coordinator-claim")
def claim_coordinator_pending(req: CoordinatorClaimReq | None = None) -> dict[str, Any]:
    body = req or CoordinatorClaimReq()
    res = store.claim_pending(
        worker_id=body.workerId or "desktop_coordinator",
        limit=max(1, min(int(body.limit or 2), 4)),
        shot_ids=body.shotIds,
        ai_only=True,
    )
    claimed_ids = [
        str(s.get("id"))
        for s in (res.get("claimed") or [])
        if isinstance(s, dict) and s.get("id")
    ]
    agent_presence.touch_desktop_activity(
        active_shot_ids=claimed_ids,
        ttl_sec=600.0 if claimed_ids else 120.0,
    )
    return {"ok": True, **res}


@app.get("/api/agent/locks")
def list_agent_locks() -> dict[str, Any]:
    return {"locks": resource_locks.list_active()}


@app.post("/api/agent/locks/{lock_name}/acquire")
def acquire_agent_lock(lock_name: str, req: LockAcquireReq | None = None) -> Response:
    body = req or LockAcquireReq()
    try:
        res = resource_locks.acquire(lock_name, owner=body.owner, ttl_sec=body.ttlSec, wait_sec=body.waitSec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not res.get("acquired"):
        return JSONResponse(status_code=423, content=res)
    return JSONResponse(status_code=200, content=res)


@app.post("/api/agent/locks/{lock_name}/release")
def release_agent_lock(lock_name: str, req: LockReleaseReq | None = None) -> Response:
    body = req or LockReleaseReq()
    try:
        res = resource_locks.release(lock_name, token=body.token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if res.get("conflict"):
        return JSONResponse(status_code=409, content=res)
    return JSONResponse(status_code=200, content=res)


@app.post("/api/agent/locks/{lock_name}/release-owner")
def release_agent_lock_by_owner(lock_name: str, req: LockOwnerReleaseReq | None = None) -> Response:
    body = req or LockOwnerReleaseReq()
    try:
        res = resource_locks.release_by_owner(lock_name, owner=body.owner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=200, content=res)


@app.get("/api/events/wait")
def wait_events(
    since: int = Query(default=0),
    timeout: float = Query(default=20.0),
    actionable: bool = Query(default=False),
    ai_only: bool = Query(default=False),
    fastpath_only: bool = Query(default=False),
    watcher: str = Query(default=""),
    mode: str = Query(default=""),
    watcher_token: str = Query(default=""),
    pid: int = Query(default=0),
) -> dict[str, Any]:
    clean_mode = mode.strip()
    clean_tok = watcher_token.strip()
    if clean_mode == "desktop" and clean_tok and agent_presence.is_desktop_token_superseded(clean_tok):
        return {
            "timedOut": True,
            "superseded": True,
            "eventSeq": since,
            "pendingCount": 0,
            "processingCount": 0,
            "processingShotIds": [],
            "pending": [],
        }
    if watcher.strip() and clean_mode in {"bridge", "desktop"}:
        agent_presence.record_heartbeat(
            watcher.strip(),
            clean_mode,
            pid=pid or None,
            ttl_sec=max(float(timeout or 20.0) + 15.0, 35.0),
            watcher_token=clean_tok or None,
            return_snapshot=False,
        )
    abort_fn = (
        (lambda: agent_presence.is_desktop_token_superseded(clean_tok))
        if (clean_mode == "desktop" and clean_tok)
        else None
    )
    return store.wait_for_events(
        since_seq=since,
        timeout=timeout,
        only_actionable=actionable,
        ai_only=ai_only,
        fastpath_only=fastpath_only,
        abort_check=abort_fn,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("WASH_APP_PORT", "8787"))
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False)
