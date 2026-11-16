from __future__ import annotations

import json
import mimetypes
import os
import re
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
    normalize_platform,
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


class AgentPresenceTracker:
    """In-memory heartbeat & live per-shot runtime telemetry for the agy bridge pool."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._watchers: dict[str, dict[str, Any]] = {}
        self._shot_workers: dict[str, dict[str, Any]] = {}
        self._cancel_tokens: dict[str, float] = {}
        self._bridge_meta: dict[str, Any] = {
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
    ) -> dict[str, Any]:
        wid = (watcher_id or mode or "agy_bridge").strip()[:80] or "agy_bridge"
        ttl = max(5.0, min(float(ttl_sec or 40.0), 180.0))
        now = time.monotonic()
        wall_now = time.time()
        active_clean = [str(x).strip() for x in (active_shot_ids or []) if str(x).strip()]
        with self._lock:
            self._prune_locked(now)
            self._watchers[wid] = {
                "watcherId": wid,
                "mode": "bridge",
                "busy": bool(busy),
                "activeShotIds": active_clean,
                "pid": int(pid) if pid else None,
                "expiresAt": now + ttl,
                "updatedAt": wall_now,
            }
            self._bridge_meta["busy"] = bool(busy)
            self._bridge_meta["activeShotIds"] = active_clean
            if last_conversation_id is not None:
                self._bridge_meta["lastConversationId"] = str(last_conversation_id).strip()
            if last_run_at is not None:
                self._bridge_meta["lastRunAt"] = str(last_run_at).strip()
            if last_error is not None:
                self._bridge_meta["lastError"] = str(last_error).strip()
            if isinstance(shot_workers, dict):
                next_workers: dict[str, dict[str, Any]] = {}
                for k, v in shot_workers.items():
                    if not isinstance(v, dict):
                        continue
                    sid = str(v.get("shotId") or k).strip()
                    if not sid:
                        continue
                    rec = dict(v)
                    rec["shotId"] = sid
                    rec["heartbeatMono"] = now
                    rec["heartbeatWall"] = wall_now
                    next_workers[sid] = rec
                self._shot_workers = next_workers
        return self.snapshot()

    def clear_watcher(self, watcher_id: str) -> None:
        wid = (watcher_id or "").strip()
        if not wid:
            return
        with self._lock:
            self._watchers.pop(wid, None)
            if wid == "agy_bridge":
                self._shot_workers.clear()

    def inspect_shot_runtime(self, shot: dict[str, Any]) -> dict[str, Any]:
        sid = str(shot.get("id") or "").strip()
        status = str(shot.get("status") or "").strip()
        task_type = str(shot.get("processingTaskType") or (status if status in ACTIONABLE_STATUSES else "") or "").strip()
        task_label = TASK_TYPE_LABELS.get(task_type, task_type or "后台处理")
        snap = self.snapshot()
        bridge_online = bool(snap.get("bridgeOnline"))
        bridge_paused = bool(snap.get("bridgePaused"))
        bridge_pid = snap.get("bridgePid")
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
            if not bridge_online:
                health = "stalled"
                health_label = "🔴 后台桥接进程未运行"
                diagnosis = "镜头仍标记为「处理中」，但后台桥接进程已离线；建议点「↻ 刷新重试」重新入队并启动执行。"
                suggested_action = "retry"
            elif not worker:
                if elapsed_sec >= 12:
                    health = "stalled"
                    health_label = "🔴 未检测到活跃 Worker（疑似中断）"
                    diagnosis = f"桥接虽在线，但已过 {elapsed_sec} 秒仍未找到该镜头的活跃 Worker（可能桥接曾重启中断）；点「↻ 刷新重试」可立刻重跑。"
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
            if bridge_paused:
                health = "paused"
                health_label = "⏸ 后台桥接已暂停"
                diagnosis = "后台 4 路并发桥接处于暂停状态，任务暂时不会自动领单；点击右上角状态恢复桥接或点「↻ 重试」核查。"
                suggested_action = "resume_bridge"
            elif not bridge_online:
                health = "offline"
                health_label = "🔴 后台桥接未连接"
                diagnosis = "当前任务正在队列等待，但后台桥接尚未启动；请在右上角点击「一键重连（启动桥接）」。"
                suggested_action = "resume_bridge"
            else:
                health = "queued"
                health_label = "⏳ 正在排队等待空闲槽位领单"
                active_cnt = len(snap.get("activeShotIds") or [])
                diagnosis = f"后台桥接正常在线（当前正在并发处理 {active_cnt}/{snap.get('maxWorkers', 4)} 个镜头），空闲后会立即自动领单执行。"
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
            "phase": str(worker.get("phase") or ("排队待领单" if is_waiting else ("后台处理中" if is_processing else ""))),
            "stepCount": int(worker.get("stepCount") or 0),
            "stepType": str(worker.get("stepType") or ""),
            "lastNote": str(worker.get("lastNote") or shot.get("agentSummary") or ""),
            "recentSteps": list(worker.get("recentSteps") or []),
            "model": str(worker.get("model") or ""),
            "resumed": bool(worker.get("resumed")),
            "conversationId": str(worker.get("conversationId") or shot.get("cliConversationId") or ""),
            "agyPid": agy_pid,
            "agyPidAlive": agy_pid_alive,
            "bridgeOnline": bridge_online,
            "bridgePaused": bridge_paused,
            "bridgePid": bridge_pid,
            "claimedAt": str(shot.get("claimedAt") or ""),
            "claimedBy": str(shot.get("claimedBy") or ""),
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
        with self._lock:
            self._prune_locked(now)
            watchers = list(self._watchers.values())
            bridge_watchers = [w for w in watchers if w["mode"] == "bridge"]
            bridge_online = bool(bridge_watchers) or (live_pid is not None and not paused)
            bridge_meta = dict(self._bridge_meta)
            workers_copy = {sid: dict(w) for sid, w in self._shot_workers.items()}

        processing_ids = []
        pending_count = 0
        if state_data:
            shots = state_data.get("shots")
            if isinstance(shots, list):
                processing_ids = [s["id"] for s in shots if s.get("status") == "processing"]
                pending_count = sum(1 for s in shots if s.get("status") in ACTIONABLE_STATUSES)
            else:
                processing_ids = list(state_data.get("processingShotIds") or [])
                pending_count = int(state_data.get("pendingCount", 0))

        bridge_active_ids = bridge_meta.get("activeShotIds") or []
        any_busy = bool(processing_ids or bridge_active_ids or bridge_meta.get("busy"))

        agy_installed = bool(agent_bridge.find_agy_bin())
        max_workers = int(getattr(agent_bridge, "MAX_WORKERS", 4) or 4)
        if any_busy and bridge_online:
            conn_state = "busy"
            busy_count = len(set(processing_ids) | set(bridge_active_ids)) or 1
            label = f"AI 并发处理中 ({busy_count})"
            detail = f"后台桥接正在并发处理 {busy_count} 个镜头（{', '.join(processing_ids or bridge_active_ids)}）。"
        elif paused:
            conn_state = "paused"
            label = "AI 桥接已暂停"
            detail = f"后台 {max_workers} 路并发桥接已手动暂停；点击下方「一键重连（启动桥接）」即可恢复自动领单执行。"
        elif bridge_online and not agy_installed:
            conn_state = "bridge_ready"
            label = "桥接待配置 CLI"
            detail = "本地控制台已就绪；请先安装 Antigravity 并安装/关联 `agy` 命令行工具，以启用后台并发桥接与前台接手对话。"
        elif bridge_online:
            conn_state = "bridge_ready"
            label = "AI 桥接已就绪"
            detail = f"后台 {max_workers} 路并发桥接在线（agy CLI）；每个镜头独立会话互不阻塞，也可随时一键唤起前台窗口接手。"
        else:
            conn_state = "offline"
            label = "AI 桥接未连接"
            detail = "后台并发桥接进程未运行；点击下方「一键重连（启动桥接）」即可恢复自动领单执行。"

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
            "agyInstalled": agy_installed,
            "bridgeOnline": bridge_online,
            "bridgePid": live_pid,
            "bridgePaused": paused,
            "maxWorkers": max_workers,
            "busy": any_busy,
            "activeShotIds": sorted(set(processing_ids) | set(bridge_active_ids)),
            "processingShotIds": processing_ids,
            "pendingCount": pending_count,
            "lastConversationId": bridge_meta.get("lastConversationId", ""),
            "lastRunAt": bridge_meta.get("lastRunAt", ""),
            "lastError": bridge_meta.get("lastError", ""),
            "shotWorkers": worker_summaries,
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
    claimToken: str | None = None


class ClaimReq(BaseModel):
    workerId: str = "coordinator"
    limit: int = 10
    shotIds: list[str] | None = None


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
def health() -> dict[str, Any]:
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

    if not comment.strip() and not tags and not saved_attachments and not edited_prompt.strip():
        raise HTTPException(status_code=400, detail="请至少修改提示词、填写修改意见、勾选标签或附带图片")

    try:
        shot = store.submit_feedback(shot_id, comment, tags, target_slot, saved_attachments, edited_prompt)
        return {"ok": True, "shot": shot}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
        shot = store.request_run(shot_id, edited_prompt=body.edited_prompt)
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
        "canResumeCliChat": bool(shot.get("canResumeCliChat")),
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
        res = store.update_shot_meta(shot_id, title=req.title, sync_local=bool(req.syncLocal))
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


@app.post("/api/shots/{shot_id}/sync-canvas")
@app.post("/api/shots/{shot_id}/sync-libtv")
def sync_libtv(shot_id: str) -> dict[str, Any]:
    try:
        shot = store.get_shot(shot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    plat = normalize_platform(shot.get("platform"))
    plat_label = get_platform_label(plat)
    shot_dir = SHOTS_DIR / shot["id"]
    try:
        if plat == "jimeng":
            info = jimeng_bridge.sync_shot_node(shot, shot_dir)
        else:
            info = libtv_bridge.sync_shot_node(shot, shot_dir)
    except (jimeng_bridge.JimengAuthRequiredError, libtv_bridge.LibtvAuthRequiredError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"查询或下载 {plat_label} 节点失败: {exc}") from exc

    updated = store.update_shot_from_libtv_sync(
        shot["id"],
        prompt_text=info.get("promptText") or None,
        mixed_order=info.get("mixedOrder"),
        draft_path=info.get("draftPath"),
        remote_draft_url=info.get("remoteUrl") or "",
        watermark=info.get("watermark"),
        node_key=info.get("nodeKey"),
        resource_id=str(info.get("resourceId") or ""),
        final_path=info.get("finalPath"),
        remote_final_url=info.get("finalRemoteUrl") or "",
        final_watermark=info.get("finalWatermark"),
        final_node_key=info.get("finalNodeKey"),
        final_resource_id=str(info.get("finalResourceId") or ""),
    )
    return {
        "ok": True,
        "platform": plat,
        "shot": updated,
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
        shot = store.agent_update_shot(shot_id, req.model_dump(exclude_unset=True))
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
    )
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


@app.post("/api/agent/claim")
def claim_agent_pending(req: ClaimReq | None = None) -> dict[str, Any]:
    body = req or ClaimReq()
    res = store.claim_pending(worker_id=body.workerId, limit=body.limit, shot_ids=body.shotIds)
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
    watcher: str = Query(default=""),
    mode: str = Query(default=""),
) -> dict[str, Any]:
    if watcher.strip() and mode.strip() == "bridge":
        agent_presence.record_heartbeat(
            watcher.strip(),
            "bridge",
            ttl_sec=max(float(timeout or 20.0) + 15.0, 35.0),
        )
    return store.wait_for_events(since_seq=since, timeout=timeout, only_actionable=actionable)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("WASH_APP_PORT", "8787"))
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False)
