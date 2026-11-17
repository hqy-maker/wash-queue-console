#!/usr/bin/env python3
"""Standalone headless Antigravity (`agy`) queue bridge for Wash Queue Console.

Runs alongside `server.py` so queued shots, feedback, prompt sync, recheck, run,
and 1080P promote tasks are dispatched concurrently across isolated `agy -p`
workers (default 4 workers, configurable 1..6 via `WASH_BRIDGE_WORKERS` or
`~/.wash_console/bridge.json`).

Performance & safety features:
1. Per-shot conversation continuity (`--conversation <id>`) on follow-up tasks
   with automatic clean retry if a resumed conversation is missing/expired.
2. Deterministic Python fast-path for pure prompt syncs (`prompt_update_requested`)
   plus pre-computed PyAV keyframe contact sheet, audio RMS scan, and canvas node/Y
   pre-prep before waking `agy -p`.
3. Tiered `--effort` (low/medium/high) and safe optional lightweight `--model` routing
   for maintenance tasks vs creative prompt tasks.
4. Full `pendingFeedbacks[]` delivery and shared `chrome_ui` mutex locking for both
   LibTV and Dreamina Chrome depth-capture interactions.
"""

from __future__ import annotations

import argparse
import codecs
from concurrent.futures import Future, ThreadPoolExecutor
import fcntl
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

import shot_fastpath
from store import DATA_ROOT, WORKSPACE_ROOT, diagnose_error

HOME_DIR = Path.home()
LOCK_PATH = DATA_ROOT / "bridge.lock"
PID_PATH = DATA_ROOT / "bridge.pid"
SHOT_CONVS_PATH = DATA_ROOT / "shot_conversations.json"
BRIDGE_CONF_PATH = HOME_DIR / ".wash_console" / "bridge.json"


def find_agy_bin() -> str | None:
    candidates = [
        os.environ.get("AGY_BIN", "").strip(),
        shutil.which("agy") or "",
        str(HOME_DIR / ".local" / "bin" / "agy"),
        str(HOME_DIR / ".gemini" / "antigravity" / "bin" / "agy"),
        "/Applications/Antigravity.app/Contents/Resources/app/bin/agy",
        str(HOME_DIR / "Applications" / "Antigravity.app/Contents/Resources/app/bin/agy"),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


def _load_bridge_settings() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if BRIDGE_CONF_PATH.is_file():
        try:
            loaded = json.loads(BRIDGE_CONF_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except Exception:
            raw = {}

    def _int_opt(conf_key: str, env_key: str, default: int, lo: int, hi: int) -> int:
        val = os.environ.get(env_key, "").strip() or str(raw.get(conf_key) or "").strip()
        try:
            num = int(val) if val else default
        except ValueError:
            num = default
        return max(lo, min(num, hi))

    def _bool_opt(conf_key: str, env_key: str, default: bool) -> bool:
        ev = os.environ.get(env_key, "").strip().lower()
        if ev in {"1", "true", "yes", "on"}:
            return True
        if ev in {"0", "false", "no", "off"}:
            return False
        if conf_key in raw:
            return bool(raw[conf_key])
        return default

    def _str_opt(conf_key: str, env_key: str, default: str = "") -> str:
        return (os.environ.get(env_key, "").strip() or str(raw.get(conf_key) or "").strip() or default)

    return {
        "workers": _int_opt("workers", "WASH_BRIDGE_WORKERS", 2, 1, 6),
        "timeoutSec": _int_opt("timeoutSec", "WASH_BRIDGE_AGY_TIMEOUT", 1500, 120, 3600),
        "idleTimeoutSec": _int_opt("idleTimeoutSec", "WASH_BRIDGE_IDLE_TIMEOUT", 300, 60, 1200),
        "heavyIdleTimeoutSec": _int_opt("heavyIdleTimeoutSec", "WASH_BRIDGE_HEAVY_IDLE_TIMEOUT", 540, 120, 1500),
        "orphanIdleSec": _int_opt("orphanIdleSec", "WASH_BRIDGE_ORPHAN_IDLE", 65, 30, 600),
        "autoRecoverStalls": _bool_opt("autoRecoverStalls", "WASH_BRIDGE_AUTO_RECOVER", True),
        "resumeByShot": _bool_opt("resumeByShot", "WASH_BRIDGE_RESUME_SHOT", True),
        "fastPathEnabled": _bool_opt("fastPathEnabled", "WASH_BRIDGE_FASTPATH", True),
        "prePrepEnabled": _bool_opt("prePrepEnabled", "WASH_BRIDGE_PREPREP", True),
        "heavyModel": _str_opt("heavyModel", "WASH_BRIDGE_HEAVY_MODEL", ""),
        "fastModel": _str_opt("fastModel", "WASH_BRIDGE_FAST_MODEL", ""),
        "heavyEffort": _str_opt("heavyEffort", "WASH_BRIDGE_HEAVY_EFFORT", "high"),
        "creationEffort": _str_opt("creationEffort", "WASH_BRIDGE_CREATION_EFFORT", "medium"),
        "feedbackResumeEffort": _str_opt("feedbackResumeEffort", "WASH_BRIDGE_FEEDBACK_RESUME_EFFORT", "medium"),
        "midEffort": _str_opt("midEffort", "WASH_BRIDGE_MID_EFFORT", "medium"),
        "recheckEffort": _str_opt("recheckEffort", "WASH_BRIDGE_RECHECK_EFFORT", "medium"),
        "fastEffort": _str_opt("fastEffort", "WASH_BRIDGE_FAST_EFFORT", "low"),
        "jimengCreditCeiling": _int_opt("jimengCreditCeiling", "WASH_JIMENG_CREDIT_CEILING", 0, 0, 100000),
    }


SETTINGS = _load_bridge_settings()
AGY_BIN = find_agy_bin() or str(HOME_DIR / ".local" / "bin" / "agy")
MAX_WORKERS = SETTINGS["workers"]
AGY_TIMEOUT_SEC = SETTINGS["timeoutSec"]

_STOP_EVENT = threading.Event()
_LOCK_FH: Any = None
_MODELS_LOCK = threading.Lock()
_CACHED_AGY_MODELS: set[str] | None = None


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{ts}] [bridge] {msg}", flush=True)


def _discover_agy_models(agy_bin: str) -> set[str]:
    global _CACHED_AGY_MODELS
    with _MODELS_LOCK:
        if _CACHED_AGY_MODELS is not None:
            return set(_CACHED_AGY_MODELS)
        found: set[str] = set()
        try:
            proc = subprocess.run(
                [agy_bin, "models"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=12,
            )
            for line in (proc.stdout or "").splitlines():
                clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line).strip()
                if not clean or clean.startswith("Fetching"):
                    continue
                first = clean.split()[0]
                if re.match(r"^[a-zA-Z0-9._-]+$", first):
                    found.add(first)
        except Exception:
            found = set()
        _CACHED_AGY_MODELS = found
        return set(found)


def _normalize_effort(effort: str, default: str = "medium") -> str:
    clean = (effort or "").strip().lower()
    if clean == "extra":
        clean = "xhigh"
    return clean if clean in {"low", "medium", "high", "xhigh", "max"} else default


def _resolve_compatible_effort(model: str, effort: str) -> str:
    """Align `--effort` with model-encoded tier suffixes to prevent CLI conflicts."""
    clean_model = (model or "").strip().lower()
    clean_effort = _normalize_effort(effort, default="")
    for tier in ("-xhigh", "-high", "-medium", "-low"):
        if clean_model.endswith(tier):
            return tier.lstrip("-")
    return clean_effort


def _pick_hydrogen_model(target_effort: str, available_models: set[str]) -> str:
    """Pick the matching Gemini 4 Hydrogen model for `target_effort` (no 3.8 Flash)."""
    eff = _normalize_effort(target_effort, default="medium")
    candidates = {
        "xhigh": ("gemini-4-hydrogen-xhigh", "gemini-4-hydrogen-high", "gemini-4-carbon-high"),
        "high": ("gemini-4-hydrogen-high", "gemini-4-hydrogen-xhigh", "gemini-4-carbon-high"),
        "medium": ("gemini-4-hydrogen-medium", "gemini-4-hydrogen-low", "gemini-4-carbon-medium", "gemini-4-argon-medium"),
        "low": ("gemini-4-hydrogen-low", "gemini-4-hydrogen-medium", "gemini-4-carbon-low", "gemini-4-argon-low"),
    }.get(eff, ("gemini-4-hydrogen-xhigh", "gemini-4-hydrogen-high", "gemini-4-hydrogen-medium"))
    for cand in candidates:
        if cand in available_models:
            return cand
    return ""


def _select_execution_profile(
    task_type: str,
    available_models: set[str],
    cfg: dict[str, Any],
    *,
    shot: dict[str, Any] | None = None,
    is_resume: bool = False,
) -> tuple[str, str]:
    """Return `(model, effort)` tuned per task complexity for fast turnaround."""
    if task_type == "prompt_update_requested":
        effort = _normalize_effort(cfg.get("fastEffort", "low"), default="low")
        model = cfg["fastModel"] or _pick_hydrogen_model(effort, available_models)
        return model, _resolve_compatible_effort(model, effort)

    if task_type == "recheck_requested":
        effort = _normalize_effort(cfg.get("recheckEffort") or cfg.get("fastEffort") or "medium", default="medium")
        model = cfg["fastModel"] or _pick_hydrogen_model(effort, available_models)
        return model, _resolve_compatible_effort(model, effort)

    if task_type in {"run_requested", "final_requested"}:
        effort = _normalize_effort(cfg.get("midEffort", "medium"), default="medium")
        model = cfg["heavyModel"] or _pick_hydrogen_model(effort, available_models)
        return model, _resolve_compatible_effort(model, effort)

    work_mode = str((shot or {}).get("workMode") or "video_wash").strip().lower()
    if work_mode == "video_creation":
        effort = _normalize_effort(cfg.get("creationEffort", "medium"), default="medium")
        model = cfg["heavyModel"] or _pick_hydrogen_model(effort, available_models)
        return model, _resolve_compatible_effort(model, effort)

    if task_type == "feedback_queued" and is_resume and shot:
        pending_fbs = [f for f in (shot.get("feedbacks") or []) if isinstance(f, dict) and f.get("status") == "pending"]
        has_media_attach = any(bool(f.get("attachments")) for f in pending_fbs)
        has_wo = bool(((shot.get("whiteoutOptions") or {}).get("parts") or []))
        if not has_media_attach and not has_wo:
            effort = _normalize_effort(cfg.get("feedbackResumeEffort", "medium"), default="medium")
            model = cfg["heavyModel"] or _pick_hydrogen_model(effort, available_models)
            return model, _resolve_compatible_effort(model, effort)

    # Full multi-modal wash prompt writing & visual revision -> high (or user-configured heavyEffort)
    effort = _normalize_effort(cfg.get("heavyEffort", "high"), default="high")
    model = cfg["heavyModel"] or _pick_hydrogen_model(effort, available_models)
    return model, _resolve_compatible_effort(model, effort)


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
    "queued": "请严格按 `wash-queue-console` §3.A（queued 新建排片）执行：先查看 `prepped` 里已生成好的 `sourceKeyframesPath` 与 `sourceAudioSummary`（含明显语音时再用 `read_file` 听 `sourceVideo`），按需涂白、写提示词并落盘 `outputs/{shot_id}_提示词.md`、上传/深度动作捕捉/连线 LibTV（不自动点击生成），完成后回写 `ready_on_canvas` 并提供清晰的 `revisionDirection`（概括首版核心思路与保留锚点）。",
    "feedback_queued": "请严格按 `wash-queue-console` §3.B（feedback_queued 修改意见 / 涂白 / @新图 / 改词）处理，务必逐条落实 `pendingFeedbacks[]` 中的**全部待处理意见**（不止最后一条），遵循 `seedance-video-prompts` 增量修改铁律（仅修改用户提及处），同步至 LibTV 节点与 `outputs/{shot_id}_提示词.md`，完成后回写 `ready_on_canvas`、置 `resolveFeedbacks: true`，并**必填** `revisionDirection`。",
    "recheck_requested": "请严格按 `wash-queue-console` §3.C（recheck_requested 中断恢复与全链路自检）核对画布节点/连线/涂白/提示词/序号对齐并修复，完成后恢复合适状态并在 `revisionDirection` / `agentSummary` 汇报核查修改方向与结果。",
    "final_requested": "请严格按 `wash-queue-console` §3.D（final_requested 转 1080P 正片并下载归档）运行 `libtv video final` 并下载至 `shots/{shot_id}/final_1080p.mp4`，完成多模态视听核验后回写 `final_ready`。",
    "run_requested": "请严格按 `wash-queue-console` §3.E（run_requested 执行 Seedance 生成并下载看片）同步待更新提示词（若有）→ 执行 `libtv node ... --run` 等待终态 → 拉回无水印 480P 样片到本地 `draft_480p.mp4` → 多模态直读拉片自检，完成后回写 `draft_ready` 与看片反馈 `revisionDirection`。",
    "prompt_update_requested": "请严格按 `wash-queue-console` §3.F（prompt_update_requested 同步手动改词/回滚词到画布，不要触发 --run）读取新旧提示词对照并更新 LibTV 画布节点与 `outputs/{shot_id}_提示词.md`，完成后恢复为 `promptUpdatePrevStatus`（若已解锁定版重改不得恢复为 `final_ready`）、清除 `pendingPromptEdit`，并用 `revisionDirection` 归纳本次手动调词的核心改动方向。",
}


JIMENG_TASK_SECTION_HINTS = {
    "queued": "请严格按 Fast-Path 预建流执行：优先直接复用 `prepped` 中已预上传并定位好的 `preuploadedCompNodeId`、`preuploadedCharRefNodes`（含 `公共-<角色>`）、`preuploadedSourceNodeId` 及 `pretriggeredDepthNodeId`（无需重扫全画布、重复上传或重开浏览器点深度）；用 `read_file` 看 `prepped.sourceKeyframesPath`（有明显声波才听 `prepped.sourceAudioPath`）并目视 `compImage` 决定是否跑 `whiteout_mask.py` 涂白替换；写出高水准提示词并落盘 `outputs/{shot_id}_提示词.md`，确认景深视频生成就绪后创建 `{shot_id}-样片`（`--mode m2v --model seedance_2.5_draft --ratio 21:9 --resolution 480p --duration <整数秒> --count 1 --x 3620 --y <suggestedRowY>`，严禁传 `--run`）并完成只读 `node quote` 报价，最后回写 `ready_on_canvas`、`jimengMeta` 与 `revisionDirection`。",
    "feedback_queued": "请严格按 `wash-queue-console` + `dreamina-canvas-cli` 处理即梦官网画布修改意见：**必须逐条核对并落实 `pendingFeedbacks[]` 中的全部未处理意见**（例如既有手涂涂白替换，又紧接着补充了细节修改意见），遵循增量修改原则，上传新素材并替换连线与 `dreamina-canvas node edit`（显式 `--project-id`）同步至即梦节点与 `outputs/{shot_id}_提示词.md`，保留既有已归档正片元字段，回写 `ready_on_canvas`、`resolveFeedbacks: true` 与 `revisionDirection`。",
    "recheck_requested": "请用 `dreamina-canvas node show` 与 `schema` 核对即梦官网画布节点/连线/参考顺序/提示词规范并补全同步，恢复合适状态并在 `revisionDirection` / `agentSummary` 汇报结果。",
    "final_requested": "请用 `dreamina-canvas node promote video-sample` 为即梦官网样片转 1080P 正片、下载归档并完成画面与音频全量质检后回写 `final_ready`；若 `brief.jimengCreditCeiling > 0` 且报价在该上限内，直接传 `--credit-ceiling <ceiling> --yes` 自动确认执行（禁止无故停在 `needs_confirmation`），仅在超限或无法报价时才停下询问。",
    "run_requested": "请严格遵循 `dreamina-canvas-cli` 报价与积分确认规则：先核对待更新提示词并用 `dreamina-canvas node quote` 查看即梦积分报价；若 `brief.jimengCreditCeiling > 0` 且总价在该上限内（且余额充足、非 `occupancy_unknown`），**直接执行 `node confirm --credit-ceiling <ceiling>` + `node run --credit-token <token>`** 放行生成（禁止再停回 `needs_confirmation` 询问）；仅在超限或 `jimengCreditCeiling == 0` 时停下保存 `submitId`；出片后下载并多模态自检，回写 `draft_ready`（提醒样片最长保存 7 天）。",
    "prompt_update_requested": "请对照 `promptText` 与 `previousPromptText`，通过 `dreamina-canvas node edit video`（显式 `--project-id` 与完整 generation/ref 参数，不触发 `node run`）将改词同步回即梦官网画布节点与 `outputs/{shot_id}_提示词.md`，完成后恢复为 `promptUpdatePrevStatus`（若已有 `finalModifiedReopened` 标记不得回写 `final_ready`）并清除 `pendingPromptEdit`。",
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


def _build_worker_prompt(
    shot: dict[str, Any],
    base_url: str,
    *,
    prepped: dict[str, Any] | None = None,
    is_resume: bool = False,
) -> str:
    task_type = str(shot.get("processingTaskType") or shot.get("status") or "queued")
    platform = str(shot.get("platform") or "libtv").strip().lower()
    is_jimeng = platform == "jimeng"
    work_mode = str(shot.get("workMode") or "video_wash").strip().lower()
    is_creation = work_mode == "video_creation"
    jm = shot.get("jimengMeta") or {}
    if task_type == "queued" and is_jimeng and isinstance(jm, dict) and jm.get("pendingAction") == "promote_1080p":
        task_type = "final_requested"

    wash_skill_md = _resolve_skill_path("wash-queue-console")
    dreamina_skill_md = _resolve_skill_path("dreamina-canvas-cli")
    if is_creation and task_type in {"queued", "recheck_requested", "feedback_queued"}:
        if task_type == "feedback_queued":
            dur_tip = f"（目标时长严格保持 {int(shot['targetDuration'])}s）" if shot.get("targetDuration") else ""
            section_hint = (
                f"【正常生成模式 · 增量修订{dur_tip}】仅按 `seedance-video-prompts` 增量修改铁律逐条落实 `pendingFeedbacks[]`，同步至"
                + ("即梦节点与 `outputs/{shot_id}_提示词.md`（不触发深度捕捉，也不自动生成），回写 `ready_on_canvas` 与 `resolveFeedbacks: true`。" if is_jimeng else "LibTV 节点与 `outputs/{shot_id}_提示词.md`，回写 `ready_on_canvas` 与 `resolveFeedbacks: true`。")
            )
        else:
            section_hint = (
                "【正常生成模式 · 跳过洗片与深度捕捉】"
                "仅按 `seedance-video-prompts` 规范（结合用户输入与可选构图/角色参考图；无图时纯文生）撰写 Seedance 2.5 提示词，并在即梦画布创建 480P 样片节点（纯文生 `--mode t2v`，带参考图 `--mode m2v`；只读报价、不代点生成），回写状态为 ready_on_canvas。"
                if is_jimeng
                else "【正常生成模式 · 跳过洗片与深度捕捉】"
                "仅按 `seedance-video-prompts` 与 `libtv-cli` 规范撰写提示词并搭建 LibTV Seedance 2.5 视频生成节点（无需深度动作捕捉），回写状态为 ready_on_canvas。"
            )
    else:
        section_hint = (
            JIMENG_TASK_SECTION_HINTS.get(task_type)
            if is_jimeng
            else TASK_SECTION_HINTS.get(task_type)
        ) or "请按 wash-queue-console 协议完整处理当前任务。"

    all_fbs = shot.get("feedbacks") or []
    pending_fbs = [f for f in all_fbs if isinstance(f, dict) and f.get("status") == "pending"]
    if not pending_fbs and isinstance(shot.get("latestFeedback"), dict):
        pending_fbs = [shot["latestFeedback"]]

    prepped_in = prepped or {}
    raw_brief: dict[str, Any] = {
        "id": shot.get("id"),
        "scene": shot.get("scene"),
        "platform": platform,
        "workMode": work_mode,
        "targetDuration": shot.get("targetDuration"),
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
        "sourceVideo": None if is_creation else (shot.get("sourceVideo") or {}).get("filePath"),
        "compImage": (shot.get("compImage") or {}).get("filePath"),
        "charRefs": shot.get("charRefs"),
        "promptFilePath": shot.get("promptFilePath"),
        "pendingFeedbacks": pending_fbs,
        "latestFeedback": shot.get("latestFeedback"),
        "prepped": prepped_in,
    }
    if is_jimeng:
        raw_brief["jimengMeta"] = shot.get("jimengMeta")
        raw_brief["jimengCreditCeiling"] = (
            int(shot["jimengCreditCeiling"])
            if shot.get("jimengCreditCeiling") is not None
            else int(SETTINGS.get("jimengCreditCeiling") or 0)
        )
        if not is_creation and not prepped_in.get("pretriggeredDepthNodeId"):
            raw_brief["depthCdpScriptPath"] = str(Path(__file__).resolve().parent / "scripts" / "dreamina_depth_cdp.py")
    else:
        raw_brief["libtv"] = shot.get("libtv")
        raw_brief["libtvGroupKey"] = shot.get("libtvGroupKey")
        raw_brief["libtvSampleNodeKey"] = shot.get("libtvSampleNodeKey")
        raw_brief["libtvFinalNodeKey"] = shot.get("libtvFinalNodeKey")
        raw_brief["libtvMixedOrder"] = shot.get("libtvMixedOrder")
    brief = {k: v for k, v in raw_brief.items() if v not in (None, "", [], {})}

    if is_creation:
        chrome_lock_rule = (
            "本镜为「正常生成（`video_creation`）」模式：**禁止**打开或操作 Chrome 浏览器，**禁止**触发任何深度动作捕捉（`dreamina_depth_cdp.py`）。\n"
        )
    else:
        chrome_lock_rule = (
            f"只要需要操作浏览器点击「深度动作捕捉」，务必先调用 `POST {base_url}/api/agent/locks/chrome_ui/acquire`"
            f"（`{{\"owner\": \"{shot.get('id')}\", \"ttlSec\": 60, \"waitSec\": 45}}`），点完触发后立刻 `POST {base_url}/api/agent/locks/chrome_ui/release` 释放锁，切勿与其他并行镜头争抢 Chrome 焦点。\n"
        )
    callback_rule = (
        f"关键节点务必用 `POST {base_url}/api/shots/{shot.get('id')}/agent-update`（请求体必须带 `claimToken: \"{shot.get('claimToken')}\"`）汇报进度与最终结果；"
        "若对构图图或角色图执行了局部涂白/遮罩（例如生成了 `comp_masked.png`），**必须同步传入** `whiteoutUpdates: [{\"slotTag\": \"图片1\", \"filePath\": \"<绝对路径>\", \"parts\": [...]}]`（并在即梦平台写入 `jimengMeta.maskedCompPath`）；"
        "修改或撰写提示词后**必须同时传入 `revisionDirection`** 以及 `conversationId`（若可知）；"
        "若收到 `pendingFeedbacks[]` 多条待办须**全部落实后**再置 `resolveFeedbacks: true`；"
        "若收到 HTTP 409 Conflict 说明任务已被用户新操作取代，立刻停止。\n"
    )

    if is_creation:
        lightweight_rule = (
            "6. **极速直写原则（严禁加载洗视频相关 Skill 或空转）**：\n"
            "   - **严禁**读取 `seedance-video-recreation`、`libtv-seedance-batch`、`wash-queue-console`、`dreamina-canvas-cli` 文档，**禁止**执行 `dreamina-canvas schema` / `auth status` 或抽原片帧/语音分析；\n"
            "   - 仅按 `seedance-video-prompts` 规范结合用户输入撰写提示词（纯文生 `--mode t2v` 无需读任何图；带参考图时只读相应构图/角色图，**禁止**翻读 `server.py`、`store.py`、`jimeng_bridge.py` 等控制台底层源码）。\n\n"
        )
    else:
        lightweight_rule = (
            "6. **严禁空转与消耗本机算力（极重要）**：\n"
            "   - 听音辨别对白或 SFX 时，优先直接用 `read_file` 读取极轻量的 `prepped.sourceAudioPath`（16kHz 单声道短 WAV，走云端多模态听音，零本机算力）；**严禁在本机调用 `faster-whisper` / `Whisper` / `PyTorch` / `Silero VAD` 或任何离线语音模型**！\n"
            "   - 看画面直接读取 `prepped.sourceKeyframesPath` 关键帧拼图（如需微查再读 `sourceVideo`），**禁止手写 Python/ffmpeg/PyAV 脚本重新大批量抽原片帧**；\n"
            "   - **禁止**翻读 `server.py`、`store.py`、`jimeng_bridge.py` 等控制台底层源码，所有可用字段已完整在下方 JSON 与 `wash-queue-console` / `dreamina-canvas --format json schema` 中给出。\n\n"
        )

    prepped_dict = prepped or {}
    credit_ceiling = int(brief.get("jimengCreditCeiling") or 0)
    if is_jimeng:
        if is_creation:
            skill_list = "（`seedance-video-prompts`）"
        elif task_type == "queued" and prepped_dict.get("prebuildReady"):
            skill_list = "（`dreamina-canvas-cli`、`seedance-video-recreation`、`seedance-video-prompts`）"
        else:
            skill_list = "（`dreamina-canvas-cli`、`seedance-video-recreation`、`seedance-video-prompts`）"
        if credit_ceiling > 0:
            credit_rule = (
                f"3. **已获用户明确全局积分上限授权（`{credit_ceiling}` 积分/镜）**：在 `run_requested` 或 `final_requested` 中执行扣费前，先核对报价（`node quote` 等）；只要明确报价 **`<= {credit_ceiling}`** 积分且余额充足，**严禁停在 `needs_confirmation` 打断用户**，直接调用 `dreamina-canvas node confirm --project-id <projectUuid> --node-id <nodeId> --credit-ceiling {credit_ceiling}` 获取 `--credit-token` 后紧接着 `node run`（1080P 转正传 `--credit-ceiling {credit_ceiling} --yes`）放行生成；只有超过 `{credit_ceiling}`、报 `occupancy_unknown` 或余额不足时才停回 `needs_confirmation`；`nodeId` / `submitId` / `selectedResourceId` 写入 `jimengMeta`（严禁保存 token 或凭据）；向用户交付样片时须明确提醒最长存储 7 天。\n"
            )
        else:
            credit_rule = (
                "3. 若需积分确认请严格遵循 `credits-and-hitl.md`，并将 `nodeId` / `submitId` / `selectedResourceId`（严禁保存 token 或凭据）写入 `jimengMeta`；向用户交付样片时须明确提醒最长存储 7 天。\n"
            )
        platform_rule_1 = (
            "1. 目标平台为 **即梦官网（`dreamina-canvas` CLI）**：请直接使用下方内联 `dreamina-canvas --non-interactive --format json` 命令模板并显式传入 `--project-id <projectUuid>`（不得调用未公开接口或查 `schema`）。\n"
            if is_creation
            else f"1. 目标平台为 **即梦官网（`dreamina-canvas` CLI）**：请遵循 `{dreamina_skill_md}`（命令入参以 `dreamina-canvas --format json schema` 实测为准，显式传入 `--project-id <projectUuid>`，不得调用未公开接口）。\n"
        )
        exec_rules = (
            f"{platform_rule_1}"
            f"2. {chrome_lock_rule}"
            f"{credit_rule}"
            f"4. {callback_rule}"
            "5. **禁止**在本 Worker 内启动桥接池或队列常驻监听。\n"
            f"{lightweight_rule}"
        )
    else:
        skill_list = (
            "（`seedance-video-prompts`、`libtv-cli`）"
            if is_creation
            else "（`libtv-seedance-batch`、`seedance-video-recreation`、`seedance-video-prompts`、`libtv-cli`）"
        )
        exec_rules = (
            "1. 多路并行时严禁调用 `libtv group use`，所有 `libtv` 命令显式传 `-p <projectUuid>`（与 `-g <groupKey>`）。\n"
            f"2. {chrome_lock_rule}"
            f"3. {callback_rule}"
            "4. **禁止**在本 Worker 内启动桥接池或队列常驻监听。\n"
            f"{lightweight_rule}"
        )

    prebuild_section = ""
    render_qa_only = bool(prepped_dict.get("renderFastCompleted") and prepped_dict.get("downloadedVideoPath"))
    if render_qa_only:
        dl_path = str(prepped_dict.get("downloadedVideoPath") or "")
        target_st = str(prepped_dict.get("targetFinalStatus") or "draft_ready")
        prebuild_section = (
            "## 轻量看片自检模式（Python 快速通道已完成提交、等待与下载）\n"
            f"- 严禁再次运行 `dreamina-canvas` 下单、重复报价或重复下载！成片已落盘 `{dl_path}`（规格 `{prepped_dict.get('videoResolution') or '-'}`）。\n"
            f"- 你只需执行 **1 次** `read_file` 查看 `{dl_path}`，确认主体/运镜/画质/音频无硬伤，然后立刻调用 `POST {base_url}/api/shots/{shot.get('id')}/agent-update`（带 `claimToken`、`status: \"{target_st}\"`、`resolveFeedbacks: true` 及简明 `agentSummary`）即可结束！\n\n"
        )
    elif is_jimeng and task_type == "queued" and prepped_dict:
        has_src_video = (not is_creation) and bool(brief.get("sourceVideo") or prepped_dict.get("preuploadedSourceNodeId"))
        depth_id = str(prepped_dict.get("pretriggeredDepthNodeId") or "").strip()
        depth_status = str(prepped_dict.get("depthTriggerStatus") or "").strip()
        depth_reason = str(prepped_dict.get("depthTriggerReason") or "").strip()
        row_y = int(round(float(prepped_dict.get("suggestedRowY") or 0)))
        comp_nid = str(prepped_dict.get("preuploadedCompNodeId") or "").strip()
        char_nodes = prepped_dict.get("preuploadedCharRefNodes") or []
        ordered_refs = [f"--ref node:{comp_nid}"] if comp_nid else []
        for ch in char_nodes:
            if isinstance(ch, dict) and ch.get("nodeId"):
                ordered_refs.append(f"--ref node:{ch['nodeId']}")
        if has_src_video and depth_id:
            ordered_refs.append(f"--ref node:{depth_id}")
        refs_cli_hint = " ".join(ordered_refs) if ordered_refs else ""
        pid = str(shot.get("projectUuid") or "")

        raw_target_dur = shot.get("targetDuration") if is_creation else None
        explicit_dur = int(round(float(raw_target_dur))) if raw_target_dur else 0
        if explicit_dur > 0:
            explicit_dur = max(4, min(30, explicit_dur))
        src_sug_dur = (
            ((shot.get("sourceVideo") or {}).get("meta") or {}).get("suggestedDuration")
            if not is_creation
            else None
        )
        src_sug_int = max(4, min(30, int(round(float(src_sug_dur))))) if src_sug_dur else 0
        dur_cli_token = str(explicit_dur) if explicit_dur else (str(src_sug_int) if src_sug_int else "<整数秒>")
        dur_rule_line = (
            f"- **目标时长强约束**：用户已明确指定生成时长为 **`{explicit_dur}s`**；请确保提示词动作节奏/时间戳按 `{explicit_dur}` 秒编排，并在建节点命令中严格传入 `--duration {explicit_dur}`。\n"
            if explicit_dur
            else ""
        )

        commit_script = str(Path(__file__).resolve().parent / "scripts" / "jimeng_commit_sample.py")
        py_bin = sys.executable

        if is_creation:
            creation_kind = str(prepped_dict.get("creationKind") or ("m2v" if ordered_refs else "t2v")).strip().lower()
            video_mode_flag = "t2v" if creation_kind == "t2v" or not ordered_refs else "m2v"
            gen_x = 800 if video_mode_flag == "t2v" else 3620
            refs_suffix = f" {refs_cli_hint}" if (video_mode_flag != "t2v" and refs_cli_hint) else ""
            whiteout_req = prepped_dict.get("whiteoutPartsRequested") or []
            if video_mode_flag == "t2v":
                whiteout_hint = "无构图图（纯文生视频 `--mode t2v`），无需涂白与参考节点绑定。"
            elif whiteout_req:
                whiteout_hint = f"用户勾选了涂白 `{whiteout_req}`，须用 `scripts/whiteout_mask.py` 对 `compImage` 涂白上传 `{shot.get('id')}-构图遮罩图` 并传 `--masked-comp-path` + `--whiteout-json`。"
            else:
                whiteout_hint = "用 `read_file` 目视 `compImage`：仅出现真人清晰正面五官且未遮罩时才跑 `scripts/whiteout_mask.py`；否则直接复用 `prepped.preuploadedCompNodeId`（跳过涂白与重传）。"
            prebuild_section = (
                "## Fast-Path 正常生成清单（`video_creation` · 极速直出）\n"
                f"- `prepped.prebuildReady`: `{bool(prepped_dict.get('prebuildReady'))}`，`creationKind = {video_mode_flag}`，`suggestedRowY = {row_y}`\n"
                "- 已就绪画布节点（**严禁重跑 `node find` 全表、重传素材或 `dreamina-canvas schema` / `auth status`**）：\n"
                f"  - 构图节点：`{comp_nid or '无（纯文生）'}`{f' (`(800, {row_y})`)' if comp_nid else ''}\n"
                f"  - 角色参考：`{json.dumps(char_nodes, ensure_ascii=False)}`\n"
                "- 深度捕捉：**正常生成模式已跳过**（严禁上传原片或触发 Chrome 深度捕捉）。\n"
                f"- 涂白判断：{whiteout_hint}\n"
                f"{dur_rule_line}"
                f"- 写完 `outputs/{shot.get('id')}_提示词.md` 后，**仅需 1 条命令**原子完成「建样片节点（不含 `--run`）+ 只读报价 + 回写 `ready_on_canvas`」：\n"
                f"  `{py_bin} {commit_script} --shot-id {shot.get('id')} --claim-token {shot.get('claimToken')} --project-id {pid} --prompt-file outputs/{shot.get('id')}_提示词.md --mode {video_mode_flag} --duration {dur_cli_token} --x {gen_x} --y {row_y}{refs_suffix} --base-url {base_url} --revision-direction \"<一句话说明创作思路>\"`\n\n"
            )
        else:
            if not has_src_video:
                depth_hint = "本镜未上传原片视频（纯图生视频/多图参考模式）：**无需触发或等待深度动作捕捉**，直接引用构图图与角色节点即可。"
            elif depth_id:
                depth_hint = (
                    f"已自动通过 Chrome CDP 触发 720P 深度动作捕捉，节点为 **`{depth_id}`**（状态 `{depth_status}`）：**禁止再打开/操作 Chrome 点击深度捕捉**，只需查状态 `dreamina-canvas --non-interactive --format json node show --project-id {pid} --node-id {depth_id}` 等其 `status == success`！"
                )
            else:
                depth_script_path = brief.get("depthCdpScriptPath") or str(Path(__file__).resolve().parent / "scripts" / "dreamina_depth_cdp.py")
                depth_hint = (
                    f"720P 深度捕捉尚未触发成功（`{depth_reason or 'pending'}`）：先执行 `.venv/bin/python {depth_script_path} --project-id {pid} --shot-id {shot.get('id')} --source-node-id {prepped_dict.get('preuploadedSourceNodeId', '')} --base-url {base_url}`（约 4s 完成）；仅脚本失败时才持 `chrome_ui` 锁走 Chrome MCP 手点。"
                )
            whiteout_req = prepped_dict.get("whiteoutPartsRequested") or []
            whiteout_hint = (
                f"用户勾选了涂白 `{whiteout_req}`，须用 `scripts/whiteout_mask.py` 对 `compImage` 涂白上传 `{shot.get('id')}-构图遮罩图` 并传 `--masked-comp-path` + `--whiteout-json`。"
                if whiteout_req
                else "用 `read_file` 目视 `compImage`：仅出现真人清晰正面五官且未遮罩时才跑 `scripts/whiteout_mask.py`；否则直接复用 `prepped.preuploadedCompNodeId`（跳过涂白与重传）。"
            )
            src_line = (
                f"  - 原片节点：`{prepped_dict.get('preuploadedSourceNodeId') or '未建'}` (`(1760, {row_y})`)\n"
                if has_src_video
                else "  - 原片节点：无（图+角色多模态直生视频）\n"
            )
            refs_hint_nonempty = refs_cli_hint or "--ref node:<comp> ..."
            prebuild_section = (
                "## Fast-Path 画布预建与省流清单（务必遵守以极速完成）\n"
                f"- `prepped.prebuildReady`: `{bool(prepped_dict.get('prebuildReady'))}`，`suggestedRowY = {row_y}`\n"
                "- 已就绪画布节点（**严禁重跑 `node find` 全表、重传素材或 `dreamina-canvas schema` / `auth status`**）：\n"
                f"  - 构图节点：`{comp_nid or '未建'}` (`(800, {row_y})`)\n"
                f"  - 角色参考：`{json.dumps(char_nodes, ensure_ascii=False)}`\n"
                f"{src_line}"
                f"- 深度捕捉：{depth_hint}\n"
                f"- 涂白判断：{whiteout_hint}\n"
                f"{dur_rule_line}"
                f"- 写完 `outputs/{shot.get('id')}_提示词.md` 后，**仅需 1 条命令**原子完成「建样片节点（不含 `--run`）+ 只读报价 + 回写 `ready_on_canvas`」（若更换涂白图请替换第一个 `--ref node:` 并加 `--masked-comp-path`/`--whiteout-json`）：\n"
                f"  `{py_bin} {commit_script} --shot-id {shot.get('id')} --claim-token {shot.get('claimToken')} --project-id {pid} --prompt-file outputs/{shot.get('id')}_提示词.md --mode m2v --duration {dur_cli_token} --x 3620 --y {row_y} {refs_hint_nonempty} --base-url {base_url} --revision-direction \"<一句话说明改编/保留锚点>\"`\n\n"
            )

    recreation_skill = _resolve_skill_path("seedance-video-recreation")
    prompts_skill = _resolve_skill_path("seedance-video-prompts")
    if render_qa_only:
        intro = (
            f"镜头 **{shot.get('id')}** 已由后台 Python 快速通道完成即梦生成与下载，你只需极速核查一次成片并回写结果（**无需阅读任何 Skill 文档，无需调用 Dreamina CLI**）：\n\n"
        )
    elif is_jimeng and task_type == "queued" and prepped_dict.get("prebuildReady") and not is_resume:
        if is_creation:
            intro = (
                "你是由洗片控制台唤醒的 Fast-Path 正常生成 Worker Agent（已跳过原片抽帧、音频扫描与 720P 深度捕捉）。\n"
                f"为达到极致速度：**只读 `{prompts_skill}`**，**严禁阅读 `seedance-video-recreation`、`wash-queue-console`、`dreamina-canvas-cli` 文档，禁止执行 `dreamina-canvas schema` 与 `auth status`**；写好提示词并落盘后，直接按下方内联命令建样片节点、只读报价并回写完成：\n\n"
            )
        else:
            intro = (
                "你是由洗片控制台唤醒的 Fast-Path Worker Agent（素材预上传、节点布局与 720P 深度捕捉已前置完成）。\n"
                f"为极大提速：**跳过阅读 `wash-queue-console`、`dreamina-canvas-cli` 文档，禁止执行 `dreamina-canvas schema` 与 `auth status`**；直接按 `{recreation_skill}` 与 `{prompts_skill}` 规范，看关键帧拼图/构图图/按需听轻量化音频写好提示词并落盘，随后按下方内联命令建样片节点、只读报价并回写完成：\n\n"
            )
    elif is_resume:
        intro = (
            f"你正在续接镜头 **{shot.get('id')}** 的专属独立对话（`--conversation` 已保留前序素材、剧情与画布认知，无需从头重学无关镜头）。\n"
            f"请直接处理本次新下发的 **{task_type}** 增量任务：\n\n"
        )
    elif is_creation:
        extra_cli_doc = "" if is_jimeng else f" 与 `{_resolve_skill_path('libtv-cli')}`"
        intro = (
            "你是由洗片控制台本地后台桥接器（`agent_bridge.py`）唤醒的 **正常生成（`video_creation`）** Worker Agent。\n"
            f"请只阅读 `{prompts_skill}`{extra_cli_doc}（**跳过** `seedance-video-recreation` / `libtv-seedance-batch`），然后端到端处理下方这一个已被你原子签收（claimed）的镜头：\n\n"
        )
    else:
        intro = (
            "你是由洗片控制台本地后台桥接器（`agent_bridge.py`）自动唤醒的 Worker Agent。\n"
            f"请先阅读 `{wash_skill_md}` 及本任务涉及的配套 Skill{skill_list}，然后端到端处理下方这一个已被你原子签收（claimed）的镜头：\n\n"
        )

    hdr_dur = shot.get("targetDuration") if is_creation else None
    hdr_dur_label = (
        f"{int(hdr_dur)}s（用户明确指定，须严格传 --duration {int(hdr_dur)}）"
        if hdr_dur
        else ("默认 5s / 按需 4-30s" if is_creation else "自动对齐原视频长度不变")
    )

    return (
        f"{intro}"
        f"- 工作区目录：`{WORKSPACE_ROOT}`\n"
        f"- 控制台地址：`{base_url}`\n"
        f"- 目标平台：`{'即梦官网 (jimeng)' if is_jimeng else 'LibTV (libtv)'}`\n"
        f"- 工作模式：`{'正常生成 (video_creation)' if is_creation else '洗视频 (video_wash)'}`\n"
        f"- 生成时长：`{hdr_dur_label}`\n"
        f"- 镜头 ID：`{shot.get('id')}`\n"
        f"- 任务类型：`{task_type}`\n"
        f"- `claimToken`：`{shot.get('claimToken')}`\n"
        f"- 任务指引：{section_hint}\n\n"
        "## 执行硬性约束\n"
        f"{exec_rules}"
        f"{prebuild_section}"
        "## 镜头完整元数据 JSON\n"
        f"```json\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n```\n"
    )


def _terminate_proc_silently(proc: subprocess.Popen[str] | None) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1.5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _describe_stream_event(evt: dict[str, Any]) -> tuple[str, str]:
    """Extract a concise human-readable (phase, note) pair from an agy stream-json event."""
    ev_name = str(evt.get("event") or "").strip().lower()
    if ev_name == "init":
        cid = str(evt.get("conversation_id") or "").strip()
        return "建立会话", f"已建立独立会话 {cid[:8]}…" if cid else "已建立独立会话"
    if ev_name == "step_update":
        su = evt.get("step_update") if isinstance(evt.get("step_update"), dict) else {}
        st_type = str(su.get("step_type") or "").strip()
        st_state = str(su.get("state") or "").strip()
        low = st_type.lower()
        tool_name = str(
            su.get("tool_name")
            or (su.get("tool_call") or {}).get("name")
            or su.get("name")
            or ""
        ).strip()
        if st_type == "user_input":
            return "读取任务", "已接收本镜上下文与执行规则"
        if tool_name or "tool" in low or "command" in low:
            t_label = tool_name or st_type or "工具调用"
            if st_state == "DONE":
                return "工具完成", f"已完成 {t_label}"
            return "执行工具", f"正在运行 {t_label}"
        if "think" in low or "reason" in low:
            return "分析规划", "正在推理方案与检查参数…"
        if st_type == "agent_response":
            snippet = re.sub(r"\s+", " ", str(su.get("text_delta") or "")).strip()[:64]
            if st_state == "DONE":
                return "总结回写", "Agent 已输出本轮结果"
            return "生成回应", f"正在输出：{snippet}" if snippet else "正在组织回复与执行结论…"
        return "执行步骤", f"{st_type or 'step'} ({st_state or 'ACTIVE'})"
    if ev_name == "result":
        res = evt.get("result") if isinstance(evt.get("result"), dict) else {}
        status = str(res.get("status") or "").strip() or "DONE"
        return "任务收尾", f"CLI 返回终态：{status}"
    return "", ""


SAFE_AUTO_RETRY_TASKS = {"queued", "feedback_queued", "recheck_requested", "prompt_update_requested"}


def _is_auth_error(err_msg: str) -> bool:
    low = (err_msg or "").lower()
    return any(
        k in low
        for k in (
            "authentication failed",
            "auth failed",
            "auth timed out",
            "not logged into",
            "oauth",
            "keyring",
            "unauthorized",
            "login",
            "401",
            "403",
        )
    )


def _is_auto_retryable_issue(err_msg: str) -> bool:
    low = (err_msg or "").lower()
    if not low:
        return False
    # Never auto-retry policy/auth/credits/model errors or manual cancels (check auth BEFORE "timed out"!)
    if _is_auth_error(low):
        return False
    if any(k in low for k in ("已被用户手动中止", "invalid model", "insufficient", "credit", "safety", "policy")):
        return False
    return any(
        k in low
        for k in (
            "idle_stall",
            "print timeout",
            "执行超时",
            "timeout",
            "timed out",
            "econnreset",
            "connection",
            "network",
            "502",
            "503",
            "504",
            "agy exited with code",
        )
    )


class BridgeRuntime:
    """Coordinates per-shot telemetry, sessions, fast-paths, watchdogs & parallel `agy -p` execution."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._lock = threading.Lock()
        self._conv_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._auth_cooldown_until = 0.0
        self._active: dict[str, Future[None]] = {}
        self._worker_states: dict[str, dict[str, Any]] = {}
        self._worker_procs: dict[str, subprocess.Popen[Any]] = {}
        self._cancelled_tokens: set[str] = set()
        self._auto_retried_counts: dict[str, int] = {}
        self._last_conv_id = ""
        self._last_run_at = ""
        self._last_error = ""

    def active_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._active.keys())

    def _snapshot_workers_locked(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for sid, info in self._worker_states.items():
            snap = dict(info)
            snap["recentSteps"] = list(info.get("recentSteps") or [])
            out[sid] = snap
        return out

    def _update_worker(self, shot_id: str, *, step_note: str = "", **fields: Any) -> None:
        now_wall = time.time()
        now_hms = time.strftime("%H:%M:%S", time.localtime(now_wall))
        with self._lock:
            st = self._worker_states.setdefault(
                shot_id,
                {
                    "shotId": shot_id,
                    "startedAt": now_wall,
                    "startedAtIso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_wall)),
                    "lastEventAt": now_wall,
                    "lastEventAtIso": now_hms,
                    "stepCount": 0,
                    "recentSteps": [],
                },
            )
            st.update({k: v for k, v in fields.items() if v is not None})
            st["lastEventAt"] = now_wall
            st["lastEventAtIso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_wall))
            if step_note:
                steps: list[dict[str, str]] = list(st.get("recentSteps") or [])
                last_txt = steps[-1]["text"] if steps else ""
                if step_note != last_txt:
                    steps.append({"at": now_hms, "text": step_note})
                    st["recentSteps"] = steps[-8:]
                    st["stepCount"] = int(st.get("stepCount") or 0) + 1

    def _handle_cancel_tokens(self, tokens: list[str]) -> None:
        clean = {str(t).strip() for t in tokens if str(t).strip()}
        if not clean:
            return
        procs_to_kill: list[subprocess.Popen[str]] = []
        with self._lock:
            self._cancelled_tokens.update(clean)
            if len(self._cancelled_tokens) > 200:
                self._cancelled_tokens = set(list(self._cancelled_tokens)[-100:])
            for sid, info in list(self._worker_states.items()):
                if str(info.get("claimToken") or "") in clean:
                    proc = self._worker_procs.get(sid)
                    if proc is not None:
                        procs_to_kill.append(proc)
                    info["phase"] = "正在中止"
                    info["lastNote"] = "收到用户重试/中止指令，正在清理进程…"
        for proc in procs_to_kill:
            _terminate_proc_silently(proc)

    def _load_shot_convs(self) -> dict[str, Any]:
        if not SHOT_CONVS_PATH.is_file():
            return {}
        try:
            data = json.loads(SHOT_CONVS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_shot_convs(self, data: dict[str, Any]) -> None:
        SHOT_CONVS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SHOT_CONVS_PATH.with_name(f".{SHOT_CONVS_PATH.name}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(SHOT_CONVS_PATH)

    def _get_shot_conversation_id(self, shot: dict[str, Any]) -> str:
        sid = str(shot.get("id") or "").strip()
        platform = str(shot.get("platform") or "libtv").strip().lower()
        proj_uuid = str(shot.get("projectUuid") or "").strip()
        with self._conv_lock:
            convs = self._load_shot_convs()
            rec = convs.get(sid)
            if isinstance(rec, dict):
                cid = str(rec.get("conversationId") or "").strip()
                if cid and rec.get("platform") == platform and rec.get("projectUuid", "") == proj_uuid:
                    return cid
        return str(shot.get("shotConversationId") or "").strip()

    def _set_shot_conversation_id(self, shot: dict[str, Any], conv_id: str) -> None:
        if not conv_id:
            return
        sid = str(shot.get("id") or "").strip()
        platform = str(shot.get("platform") or "libtv").strip().lower()
        proj_uuid = str(shot.get("projectUuid") or "").strip()
        with self._conv_lock:
            convs = self._load_shot_convs()
            convs[sid] = {
                "conversationId": conv_id,
                "platform": platform,
                "projectUuid": proj_uuid,
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }
            self._save_shot_convs(convs)

    def _clear_shot_conversation_id(self, shot_id: str) -> None:
        with self._conv_lock:
            convs = self._load_shot_convs()
            if shot_id in convs:
                convs.pop(shot_id, None)
                self._save_shot_convs(convs)

    def _send_heartbeat(self, client: httpx.Client) -> dict[str, Any] | None:
        ids = self.active_ids()
        with self._lock:
            last_conv = self._last_conv_id
            last_run = self._last_run_at
            last_err = self._last_error
            workers_snap = self._snapshot_workers_locked()
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
                    "shotWorkers": workers_snap,
                },
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                cancel_tokens = data.get("cancelTokens") or []
                if cancel_tokens:
                    self._handle_cancel_tokens(cancel_tokens)
                return data.get("agentStatus")
        except Exception:
            return None
        return None

    def _claim_still_ours(self, shot_id: str, claim_token: str) -> bool:
        try:
            with httpx.Client(base_url=self.base_url, timeout=8.0) as client:
                r = client.get(f"/api/shots/{shot_id}")
                if r.status_code != 200:
                    return False
                shot = r.json().get("shot") or {}
                return shot.get("status") == "processing" and shot.get("claimToken") == claim_token
        except Exception:
            return False

    def _release_owner_lock(self, owner: str, lock_name: str = "chrome_ui") -> None:
        try:
            with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
                client.post(f"/api/agent/locks/{lock_name}/release-owner", json={"owner": owner})
        except Exception:
            pass

    def _report_progress_note(self, shot_id: str, claim_token: str, summary: str) -> None:
        try:
            with httpx.Client(base_url=self.base_url, timeout=6.0) as client:
                client.post(
                    f"/api/shots/{shot_id}/agent-update",
                    json={"claimToken": claim_token, "agentSummary": summary},
                )
        except Exception:
            pass

    def _rescue_orphaned_shot(self, client: httpx.Client, shot: dict[str, Any], runtime: dict[str, Any]) -> None:
        sid = str(shot.get("id") or "")
        claim_token = str(shot.get("claimToken") or "")
        task_type = str(shot.get("processingTaskType") or "")
        if not sid or not claim_token:
            return
        self._release_owner_lock(sid)
        with self._lock:
            retried = self._auto_retried_counts.get(sid, 0)
        can_auto_retry = (
            bool(SETTINGS["autoRecoverStalls"])
            and task_type in SAFE_AUTO_RETRY_TASKS
            and retried < 1
        )
        if can_auto_retry:
            with self._lock:
                self._auto_retried_counts[sid] = retried + 1
            self._clear_shot_conversation_id(sid)
            _log(f"orphan watchdog auto-retrying {sid} (task={task_type}, idle={runtime.get('idleSec')}s)")
            client.post(
                f"/api/shots/{sid}/retry-processing",
                json={
                    "mode": "retry",
                    "actor": "system",
                    "reason": (
                        f"检测到后台 Worker 已失联（静止 {runtime.get('idleSec', 0)} 秒无存活子进程），"
                        "已自动清理锁并重新入队恢复（自动干预 1/1）…"
                    ),
                },
                timeout=8.0,
            )
            return

        _log(f"orphan watchdog marking interrupted {sid} (task={task_type}, idle={runtime.get('idleSec')}s)")
        diag = diagnose_error(
            "interrupted: 检测到后台 Worker 进程已中断（无活跃子进程认领），已自动退出假死状态。",
            failed_task=task_type,
            platform=str(shot.get("platform") or ""),
        )
        client.post(
            f"/api/shots/{sid}/agent-update",
            json={
                "claimToken": claim_token,
                "status": "error",
                "errorReason": diag["errorReason"],
                "errorSolution": diag["errorSolution"],
                "errorDetail": diag["errorDetail"],
                "agentSummary": diag["errorReason"],
                "revisionSummary": f"系统自动干预：检测到无活跃 Worker 孤儿状态（静止 {runtime.get('idleSec', 0)}s），已退出假死并标为「进程中断」",
            },
            timeout=8.0,
        )

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

    def _release_to_desktop_ai(
        self,
        shot_id: str,
        claim_token: str,
        reason: str = "",
        next_status: str = "",
    ) -> None:
        try:
            with httpx.Client(base_url=self.base_url, timeout=10.0) as client:
                body: dict[str, Any] = {"claimToken": claim_token, "reason": reason}
                if next_status:
                    body["nextStatus"] = next_status
                r = client.post(
                    f"/api/shots/{shot_id}/release-fastpath",
                    json=body,
                )
                if r.status_code not in {200, 409}:
                    _log(f"{shot_id} release-fastpath unexpected status={r.status_code} body={r.text[:200]}")
        except Exception as exc:
            _log(f"{shot_id} release-fastpath error: {exc}")

    def _invoke_agy_once(
        self,
        shot: dict[str, Any],
        prompt: str,
        *,
        agy_bin: str,
        model: str = "",
        effort: str = "",
        resume_conv_id: str = "",
        claim_token: str = "",
    ) -> tuple[str, str, str]:
        shot_id = str(shot["id"])
        env = os.environ.copy()
        extra_path = f"{HOME_DIR / '.local' / 'bin'}:{HOME_DIR / '.libtv'}:/opt/homebrew/bin:/usr/local/bin"
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"

        cmd = [agy_bin]
        if resume_conv_id:
            cmd.extend(["--conversation", resume_conv_id])
        if model:
            cmd.extend(["--model", model])
        if effort in {"low", "medium", "high", "xhigh", "max"}:
            cmd.extend(["--effort", effort])
        cmd.extend(
            [
                "-p",
                prompt,
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--print-timeout",
                f"{AGY_TIMEOUT_SEC}s",
            ]
        )

        conv_id = ""
        err_msg = ""
        last_status = ""
        proc: subprocess.Popen[Any] | None = None
        last_step_append_ts = 0.0
        task_type = str(shot.get("processingTaskType") or "")
        current_phase = "启动 agy 进程"
        spawn_lock_held = False
        spawn_auth_settled = False
        try:
            # Stagger concurrent agy cold starts so macOS Keyring LoadStoredToken never exceeds its 5s timeout under contention
            self._spawn_lock.acquire()
            spawn_lock_held = True
            spawn_deadline = time.monotonic() + 12.0
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
            )
            with self._lock:
                self._worker_procs[shot_id] = proc
            self._update_worker(
                shot_id,
                agyPid=proc.pid,
                phase=current_phase,
                lastNote=f"已拉起后台 Worker (PID {proc.pid})",
                step_note=f"启动 agy 进程 (PID {proc.pid}, model={model or 'default'})",
            )
            start_m = time.monotonic()
            last_output_m = start_m
            deadline = start_m + AGY_TIMEOUT_SEC + 60.0
            assert proc.stdout is not None
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            line_buf = ""
            sel = selectors.DefaultSelector()
            sel.register(fd, selectors.EVENT_READ)

            def _release_spawn_lock_once() -> None:
                nonlocal spawn_lock_held
                if spawn_lock_held:
                    spawn_lock_held = False
                    try:
                        self._spawn_lock.release()
                    except RuntimeError:
                        pass

            def _consume_line(raw_text: str) -> None:
                nonlocal conv_id, err_msg, last_status, last_step_append_ts, current_phase, spawn_auth_settled
                line = raw_text.strip()
                if not line:
                    return
                if line.startswith("{"):
                    try:
                        evt = json.loads(line)
                        if evt.get("conversation_id") and not conv_id:
                            conv_id = str(evt["conversation_id"])
                            with self._lock:
                                self._last_conv_id = conv_id
                            self._set_shot_conversation_id(shot, conv_id)
                            self._update_worker(shot_id, conversationId=conv_id)
                            _log(f"{shot_id} agy conversation_id={conv_id} (resumed={bool(resume_conv_id)})")
                        if not spawn_auth_settled and evt.get("event") in {"init", "step_update", "message", "result"}:
                            spawn_auth_settled = True
                            _release_spawn_lock_once()
                        phase_str, note_str = _describe_stream_event(evt)
                        if phase_str:
                            current_phase = phase_str
                        if phase_str or note_str:
                            now_m = time.monotonic()
                            su = evt.get("step_update") if isinstance(evt.get("step_update"), dict) else {}
                            step_type_str = str(su.get("step_type") or "")
                            should_append = bool(note_str) and (
                                evt.get("event") in {"init", "result"}
                                or phase_str in {"读取任务", "执行工具", "工具完成"}
                                or (now_m - last_step_append_ts) >= 8.0
                            )
                            if should_append:
                                last_step_append_ts = now_m
                            self._update_worker(
                                shot_id,
                                phase=phase_str or None,
                                stepType=step_type_str or None,
                                lastNote=note_str or None,
                                step_note=note_str if should_append else "",
                            )
                        if evt.get("event") == "result":
                            res_obj = evt.get("result") or {}
                            last_status = str(res_obj.get("status") or "")
                            if last_status != "SUCCESS":
                                err_msg = str(res_obj.get("error") or last_status or "unknown_error")
                    except json.JSONDecodeError:
                        self._update_worker(shot_id, lastNote=line[:90])
                else:
                    self._update_worker(shot_id, lastNote=line[:90])

            try:
                eof_reached = False
                while not eof_reached:
                    if _STOP_EVENT.is_set():
                        err_msg = "后台桥接服务正在停止"
                        _terminate_proc_silently(proc)
                        break
                    with self._lock:
                        cancelled = bool(claim_token and claim_token in self._cancelled_tokens)
                    if cancelled:
                        err_msg = "已被用户手动中止重置"
                        _terminate_proc_silently(proc)
                        break

                    now_m = time.monotonic()
                    if spawn_lock_held and (spawn_auth_settled or now_m >= spawn_deadline or proc.poll() is not None):
                        _release_spawn_lock_once()
                    if now_m > deadline:
                        err_msg = f"执行超时（超过 {AGY_TIMEOUT_SEC}s）"
                        _terminate_proc_silently(proc)
                        break

                    idle_elapsed = now_m - last_output_m
                    idle_limit = (
                        SETTINGS["heavyIdleTimeoutSec"]
                        if (task_type in {"run_requested", "final_requested"} and current_phase == "执行工具")
                        else SETTINGS["idleTimeoutSec"]
                    )
                    if idle_elapsed >= idle_limit:
                        _log(f"{shot_id} idle watchdog triggered after {int(idle_elapsed)}s without output (phase={current_phase}); terminating")
                        err_msg = f"idle_stall: 超过 {int(idle_elapsed)} 秒无新动作输出（阶段：{current_phase}），已由卡住看门狗自动中止"
                        _terminate_proc_silently(proc)
                        break

                    events = sel.select(timeout=2.0)
                    if not events:
                        if proc.poll() is not None and (time.monotonic() - last_output_m) >= 3.0:
                            eof_reached = True
                        continue

                    while True:
                        try:
                            chunk = os.read(fd, 65536)
                        except BlockingIOError:
                            break
                        if not chunk:
                            eof_reached = True
                            break
                        last_output_m = time.monotonic()
                        line_buf += decoder.decode(chunk, final=False)
                        while "\n" in line_buf:
                            raw_line, line_buf = line_buf.split("\n", 1)
                            _consume_line(raw_line)
                line_buf += decoder.decode(b"", final=True)
                if line_buf.strip():
                    _consume_line(line_buf)
            finally:
                sel.close()
            ret = proc.wait(timeout=15)
            with self._lock:
                if claim_token and claim_token in self._cancelled_tokens and not err_msg:
                    err_msg = "已被用户手动中止重置"
            if ret != 0 and not err_msg:
                err_msg = f"agy exited with code {ret}"
        except Exception as exc:
            err_msg = str(exc)
            _terminate_proc_silently(proc)
        finally:
            with self._lock:
                if self._worker_procs.get(shot_id) is proc:
                    self._worker_procs.pop(shot_id, None)
            if spawn_lock_held:
                spawn_lock_held = False
                try:
                    self._spawn_lock.release()
                except RuntimeError:
                    pass
        return conv_id, err_msg, last_status

    def _run_shot_worker(self, shot: dict[str, Any], ai_mode: str = "desktop_subagent") -> None:
        shot_id = str(shot["id"])
        claim_token = str(shot.get("claimToken") or "")
        task_type = str(shot.get("processingTaskType") or "")
        _log(f"start worker shot={shot_id} task={task_type} mode={ai_mode} token={claim_token}")
        self._update_worker(
            shot_id,
            claimToken=claim_token,
            taskType=task_type,
            phase="签收任务",
            lastNote=f"开始处理 {task_type} 任务…",
            step_note=f"Worker 已签收 {task_type} 任务",
        )

        conv_id = ""
        err_msg = ""
        last_status = ""
        released_to_ai = False
        fp_reason = ""
        release_next_status = ""
        try:
            prepped: dict[str, Any] = {}
            if SETTINGS["fastPathEnabled"] and task_type == "prompt_update_requested":
                self._update_worker(shot_id, phase="快速同步提示词", lastNote="直接同步提示词到画布节点…", step_note="执行确定性提示词快速同步")
                fp_res = shot_fastpath.try_fastpath_prompt_update(
                    shot, base_url=self.base_url, workspace_root=WORKSPACE_ROOT
                )
                if fp_res.get("handled"):
                    _log(f"{shot_id} fast-path completed: {fp_res.get('summary')}")
                    return
                fp_reason = str(fp_res.get("reason") or "prompt_update Fast-Path 跳过")
                _log(f"{shot_id} fast-path skipped ({fp_reason})")
            elif (
                SETTINGS["fastPathEnabled"]
                and str(shot.get("platform") or "").lower() == "jimeng"
                and task_type in {"run_requested", "final_requested"}
            ):
                def _fp_progress(ph: str, msg: str) -> None:
                    self._update_worker(shot_id, phase=ph, lastNote=msg, step_note=msg)

                def _fp_cancelled() -> bool:
                    with self._lock:
                        return _STOP_EVENT.is_set() or (bool(claim_token) and claim_token in self._cancelled_tokens)

                fp_fn = (
                    shot_fastpath.try_fastpath_jimeng_final
                    if task_type == "final_requested"
                    else shot_fastpath.try_fastpath_jimeng_run
                )
                fp_res = fp_fn(
                    shot,
                    base_url=self.base_url,
                    workspace_root=WORKSPACE_ROOT,
                    progress_cb=_fp_progress,
                    check_cancelled=_fp_cancelled,
                )
                if fp_res.get("handled"):
                    _log(f"{shot_id} {task_type} fast-path completed: {fp_res.get('summary')}")
                    return
                if isinstance(fp_res.get("preppedOverride"), dict) and ai_mode == "cli_bridge":
                    prepped = dict(fp_res["preppedOverride"])
                elif isinstance(fp_res.get("preppedOverride"), dict) and fp_res["preppedOverride"].get("renderFastCompleted"):
                    ov = fp_res["preppedOverride"]
                    release_next_status = "recheck_requested"
                    target_st = str(ov.get("targetFinalStatus") or ("final_ready" if task_type == "final_requested" else "draft_ready"))
                    dl_path = str(ov.get("downloadedVideoPath") or "")
                    fp_reason = (
                        f"⚡ 样片/正片已由 Fast-Path 生成并下载至 {dl_path}，请桌面子对话仅用 read_file 完成视听自检后"
                        f"回写 status={target_st}（切勿重复触发生成）"
                    )
                    _log(f"{shot_id} {task_type} fast-path rendered; handing QA to desktop_subagent as recheck_requested")
                else:
                    fp_reason = str(fp_res.get("reason") or f"{task_type} Fast-Path 跳过")
                    _log(f"{shot_id} {task_type} fast-path skipped ({fp_reason})")

            if ai_mode != "cli_bridge":
                reason = fp_reason or f"{task_type or 'ai'} 需 AI 处理，交由桌面一镜一对话"
                _log(f"{shot_id} releasing fast-path claim to desktop_subagent (next={release_next_status or 'keep'}): {reason}")
                self._release_to_desktop_ai(
                    shot_id,
                    claim_token,
                    reason=reason,
                    next_status=release_next_status,
                )
                released_to_ai = True
                return

            if not prepped and SETTINGS["prePrepEnabled"] and task_type in {"queued", "recheck_requested"}:
                is_jimeng_queued = str(shot.get("platform") or "").lower() == "jimeng" and task_type == "queued"
                is_creation_mode = str(shot.get("workMode") or "video_wash").strip().lower() == "video_creation"
                if is_jimeng_queued and is_creation_mode:
                    prep_phase = "预建画布"
                    prep_note = "正常生成模式：跳过原片抽帧与深度捕，并发预建参考图与画布布局…"
                    prep_step = "正常生成模式：并发预建即梦画布布局"
                elif is_jimeng_queued:
                    prep_phase = "预建画布与深度捕"
                    prep_note = "并发上传素材、摆放画布并自动触发 Chrome 720P 深度捕…"
                    prep_step = "并发预建即梦画布节点并 CDP 触发 720P 深度动作捕捉"
                elif is_creation_mode:
                    prep_phase = "扫描画布"
                    prep_note = "正常生成模式：跳过原片抽帧与音频分析，扫描画布上下文…"
                    prep_step = "正常生成模式：扫描画布上下文"
                else:
                    prep_phase = "预处理素材"
                    prep_note = "生成关键帧拼图与扫描音频声波…"
                    prep_step = "预处理关键帧拼图与音频/画布信息"
                self._update_worker(
                    shot_id,
                    phase=prep_phase,
                    lastNote=prep_note,
                    step_note=prep_step,
                )
                prepped = shot_fastpath.prepare_shot_artifacts(
                    shot,
                    base_url=self.base_url,
                    preupload_source=(task_type == "queued"),
                )
                if prepped:
                    _log(
                        f"{shot_id} prepped keys={sorted(prepped.keys())} "
                        f"prebuildReady={prepped.get('prebuildReady')} "
                        f"depthStatus={prepped.get('depthTriggerStatus')}"
                    )

            agy_bin = find_agy_bin()
            if not agy_bin:
                raise FileNotFoundError("本机未检测到 Antigravity agy CLI（安装 Antigravity 后重试）")

            resume_id = ""
            if SETTINGS["resumeByShot"] and task_type != "queued":
                resume_id = self._get_shot_conversation_id(shot)

            avail_models = _discover_agy_models(agy_bin)
            if prepped.get("renderFastCompleted"):
                eff = _normalize_effort(SETTINGS.get("fastEffort", "low"), default="low")
                model = SETTINGS["fastModel"] or _pick_hydrogen_model(eff, avail_models)
                effort = _resolve_compatible_effort(model, eff)
            else:
                model, effort = _select_execution_profile(
                    task_type,
                    avail_models,
                    SETTINGS,
                    shot=shot,
                    is_resume=bool(resume_id),
                )

            _log(f"{shot_id} dispatch profile model={model or 'default'} effort={effort} resume={resume_id or 'new'}")
            self._update_worker(
                shot_id,
                model=model or "default",
                resumed=bool(resume_id),
                conversationId=resume_id,
                phase="唤醒 agy",
                lastNote=f"调度模型 {model or 'default'} ({effort})，{'续接原会话' if resume_id else '新开会话'}…",
                step_note=f"调度 agy 执行（{model or 'default'} / {effort}）",
            )
            prompt = _build_worker_prompt(shot, self.base_url, prepped=prepped, is_resume=bool(resume_id))
            conv_id, err_msg, last_status = self._invoke_agy_once(
                shot,
                prompt,
                agy_bin=agy_bin,
                model=model,
                effort=effort,
                resume_conv_id=resume_id,
                claim_token=claim_token,
            )

            # Fallback seamlessly to a fresh session if resuming an older conversation failed and claim still holds (never retry auth popups)
            with self._lock:
                user_cancelled = claim_token in self._cancelled_tokens
            if (
                not user_cancelled
                and resume_id
                and not _is_auth_error(err_msg or last_status)
                and (err_msg or (last_status and last_status != "SUCCESS"))
            ):
                if self._claim_still_ours(shot_id, claim_token):
                    _log(f"{shot_id} resume conv={resume_id} failed ({err_msg}); retrying fresh session")
                    self._clear_shot_conversation_id(shot_id)
                    self._update_worker(
                        shot_id,
                        phase="新开会话重试",
                        resumed=False,
                        lastNote="旧会话恢复失败，自动改用干净新会话重试…",
                        step_note="旧会话续接失败，切换全新会话继续执行",
                    )
                    fresh_prompt = _build_worker_prompt(shot, self.base_url, prepped=prepped, is_resume=False)
                    conv_id, err_msg, last_status = self._invoke_agy_once(
                        shot,
                        fresh_prompt,
                        agy_bin=agy_bin,
                        model=model,
                        effort=effort,
                        resume_conv_id="",
                        claim_token=claim_token,
                    )
            else:
                with self._lock:
                    user_cancelled = claim_token in self._cancelled_tokens
                    auto_retried = self._auto_retried_counts.get(shot_id, 0)
                if (
                    not user_cancelled
                    and not resume_id
                    and SETTINGS["autoRecoverStalls"]
                    and task_type in SAFE_AUTO_RETRY_TASKS
                    and auto_retried < 1
                    and (err_msg or (last_status and last_status != "SUCCESS"))
                    and _is_auto_retryable_issue(err_msg or last_status)
                    and self._claim_still_ours(shot_id, claim_token)
                ):
                    with self._lock:
                        self._auto_retried_counts[shot_id] = auto_retried + 1
                    _log(f"{shot_id} transient stall/failure ({err_msg or last_status}); auto-intervening once with clean session")
                    self._release_owner_lock(shot_id)
                    self._clear_shot_conversation_id(shot_id)
                    msg_auto = f"检测到卡住或异常中断（{err_msg or last_status}），已自动清理锁并切换干净会话重试（自动干预 1/1）…"
                    self._report_progress_note(shot_id, claim_token, msg_auto)
                    self._update_worker(
                        shot_id,
                        phase="自动干预重试",
                        resumed=False,
                        lastNote="检测到卡住/瞬时异常，已自动释放锁并重开会话重试（1/1）…",
                        step_note=f"触发自动干预重试（原因：{(err_msg or last_status)[:60]}）",
                    )
                    retry_prompt = _build_worker_prompt(shot, self.base_url, prepped=prepped, is_resume=False)
                    conv_id, err_msg, last_status = self._invoke_agy_once(
                        shot,
                        retry_prompt,
                        agy_bin=agy_bin,
                        model=model,
                        effort=effort,
                        resume_conv_id="",
                        claim_token=claim_token,
                    )
        except Exception as exc:
            err_msg = str(exc)
        finally:
            now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            with self._lock:
                was_cancelled = bool(claim_token and claim_token in self._cancelled_tokens)
                self._active.pop(shot_id, None)
                self._worker_states.pop(shot_id, None)
                self._worker_procs.pop(shot_id, None)
                self._last_run_at = now_str
                if conv_id:
                    self._last_conv_id = conv_id
                if not was_cancelled and not released_to_ai:
                    self._last_error = err_msg
                if not released_to_ai and not (err_msg or (last_status and last_status != "SUCCESS")):
                    self._auto_retried_counts.pop(shot_id, None)
            if released_to_ai:
                self._release_owner_lock(shot_id)
                _log(f"worker released shot={shot_id} to desktop_subagent")
            else:
                if was_cancelled or err_msg or (last_status and last_status != "SUCCESS"):
                    self._release_owner_lock(shot_id)
                if was_cancelled:
                    _log(f"worker cancelled by user reset shot={shot_id}")
                elif err_msg or (last_status and last_status != "SUCCESS"):
                    issue_text = err_msg or last_status
                    if _is_auth_error(issue_text):
                        with self._lock:
                            self._auth_cooldown_until = max(self._auth_cooldown_until, time.monotonic() + 30.0)
                        _log(f"auth cooldown 30s activated after {shot_id} auth error (suppressing popup loops)")
                    _log(f"worker finished with issue shot={shot_id}: {issue_text}")
                    self._ensure_not_stuck_processing(shot_id, claim_token, issue_text)
                else:
                    _log(f"worker finished shot={shot_id} conv={conv_id}")

    def run_forever(self) -> int:
        _log("bridge worker ready (fast-path always enabled; cli_bridge loaded on demand)")

        pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="agy-worker")

        def heartbeat_loop() -> None:
            with httpx.Client(base_url=self.base_url, timeout=8.0) as hb_client:
                while not _STOP_EVENT.is_set():
                    self._send_heartbeat(hb_client)
                    _STOP_EVENT.wait(3.0 if self.active_ids() else 10.0)

        hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        hb_thread.start()

        since_seq = 0
        started_m = time.monotonic()
        last_orphan_scan_m = 0.0
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

                        ai_mode = str(st.get("aiDispatchMode") or "desktop_subagent").strip()

                        now_scan_m = time.monotonic()
                        if (now_scan_m - started_m) >= 30.0 and (now_scan_m - last_orphan_scan_m) >= 15.0:
                            last_orphan_scan_m = now_scan_m
                            proc_ids = [str(x) for x in (st.get("processingShotIds") or [])]
                            active_now = set(self.active_ids())
                            for psid in proc_ids:
                                if psid in active_now:
                                    continue
                                try:
                                    r_rt = client.get(f"/api/shots/{psid}/agent-status", timeout=5.0)
                                    if r_rt.status_code != 200:
                                        continue
                                    rt_data = r_rt.json()
                                    rt_info = rt_data.get("runtime") or {}
                                    rt_shot = rt_data.get("shot") or {}
                                    if (
                                        rt_info.get("status") == "processing"
                                        and str(rt_shot.get("claimedBy") or "") == "agy_bridge"
                                        and int(rt_info.get("bridgePid") or 0) == os.getpid()
                                        and rt_info.get("agyPidAlive") is not True
                                        and int(rt_info.get("idleSec") or 0) >= SETTINGS["orphanIdleSec"]
                                    ):
                                        self._rescue_orphaned_shot(client, rt_shot, rt_info)
                                except Exception as exc:
                                    _log(f"orphan check error for {psid}: {exc}")

                        with self._lock:
                            running_count = len(self._active)
                            auth_cooling = time.monotonic() < self._auth_cooldown_until
                        slots = MAX_WORKERS - running_count

                        is_desktop_mode = ai_mode != "cli_bridge"
                        can_claim = (
                            slots > 0
                            and (
                                (is_desktop_mode and int(st.get("fastpathPendingCount", 0) or 0) > 0)
                                or (
                                    not is_desktop_mode
                                    and not auth_cooling
                                    and int(st.get("pendingCount", 0) or 0) > 0
                                    and bool(find_agy_bin())
                                )
                            )
                        )
                        if can_claim:
                            claim_payload: dict[str, Any] = {"workerId": "agy_bridge", "limit": slots}
                            if is_desktop_mode:
                                claim_payload["fastpathOnly"] = True
                            claim_resp = client.post(
                                "/api/agent/claim",
                                json=claim_payload,
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
                                            fut = pool.submit(self._run_shot_worker, s, ai_mode)
                                            self._active[sid] = fut
                                    self._send_heartbeat(client)
                                    continue

                        wait_params: dict[str, Any] = {
                            "since": since_seq,
                            "timeout": 10.0,
                            "actionable": "true",
                            "watcher": "agy_bridge",
                            "mode": "bridge",
                        }
                        if is_desktop_mode:
                            wait_params["fastpath_only"] = "true"
                        wait_resp = client.get(
                            "/api/events/wait",
                            params=wait_params,
                            timeout=14.0,
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
    parser = argparse.ArgumentParser(description="Headless agy queue bridge for Wash Queue Console")
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
    _log(
        f"started pid={os.getpid()} target={base_url} max_workers={MAX_WORKERS} "
        f"resume={SETTINGS['resumeByShot']} fastpath={SETTINGS['fastPathEnabled']} preprep={SETTINGS['prePrepEnabled']}"
    )
    try:
        runtime = BridgeRuntime(base_url)
        return runtime.run_forever()
    finally:
        _cleanup_pid_and_lock()
        _log("stopped")


if __name__ == "__main__":
    raise SystemExit(main())
