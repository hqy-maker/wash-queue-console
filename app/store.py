from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

HOME_DIR = Path.home()
APP_ROOT = Path(__file__).resolve().parent


def _load_user_console_config() -> dict[str, Any]:
    cfg_path = HOME_DIR / ".wash_console" / "config.json"
    if not cfg_path.is_file():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_workspace_root() -> Path:
    env_ws = os.environ.get("WASH_WORKSPACE_ROOT", "").strip()
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    if APP_ROOT.parent.name == "tools":
        return APP_ROOT.parent.parent.resolve()
    cfg = _load_user_console_config()
    cfg_ws = str(cfg.get("workspaceRoot") or "").strip()
    if cfg_ws:
        return Path(cfg_ws).expanduser().resolve()
    return (HOME_DIR / "Documents" / "洗片控制台工作区").resolve()


WORKSPACE_ROOT = _resolve_workspace_root()
DATA_ROOT = (
    Path(os.environ["WASH_DATA_ROOT"]).expanduser().resolve()
    if os.environ.get("WASH_DATA_ROOT", "").strip()
    else WORKSPACE_ROOT / "wash_queue_data"
)
QUEUE_PATH = DATA_ROOT / "queue.json"
PRESETS_PATH = DATA_ROOT / "presets.json"
SHOTS_DIR = DATA_ROOT / "shots"
PRESETS_DIR = DATA_ROOT / "presets"

_CANDIDATE_SCRIPT_DIRS = [
    APP_ROOT / "scripts",
    HOME_DIR / ".gemini" / "config" / "skills" / "libtv-seedance-batch" / "scripts",
]
PREFLIGHT_SCRIPTS_DIR = next((d for d in _CANDIDATE_SCRIPT_DIRS if (d / "preflight_check.py").is_file()), _CANDIDATE_SCRIPT_DIRS[0])
if str(PREFLIGHT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(PREFLIGHT_SCRIPTS_DIR))

import preflight_check  # type: ignore  # noqa: E402

SEED_ASSETS_DIR = APP_ROOT / "seed_assets"
SEED_S01_PROMPT_MD = WORKSPACE_ROOT / "__unused__"
_BRAIN_ENV = os.environ.get("WASH_BRAIN_UPLOAD_DIR", "").strip()
BRAIN_UPLOAD_DIR = Path(_BRAIN_ENV).expanduser() if _BRAIN_ENV else Path("__unused_seed_fallback__")

CLEAN_INIT_MODE = True
DEFAULT_PROJECT_UUID = ""
DEFAULT_PROJECT_NAME = "未选择 LibTV 画布"
DEFAULT_WORKSPACE_ID = ""
ALLOWED_PLATFORMS = {"libtv", "jimeng"}
DEFAULT_PLATFORM = "libtv"
JIMENG_HOME_URL = "https://jimeng.jianying.com/ai-tool/home"


def normalize_platform(raw: str | None) -> str:
    clean = (raw or "").strip().lower()
    if clean in {"jimeng", "dreamina", "即梦", "即梦官网"}:
        return "jimeng"
    return "libtv"


def get_platform_label(platform: str | None) -> str:
    return "即梦官网" if normalize_platform(platform) == "jimeng" else "LibTV"

ACTIONABLE_STATUSES = {
    "queued",
    "run_requested",
    "prompt_update_requested",
    "feedback_queued",
    "final_requested",
    "recheck_requested",
}
ALLOWED_STATUSES = {
    "drafting",
    "queued",
    "processing",
    "recheck_requested",
    "needs_confirmation",
    "ready_on_canvas",
    "prompt_update_requested",
    "run_requested",
    "draft_ready",
    "feedback_queued",
    "final_requested",
    "final_ready",
    "error",
}

TASK_TYPE_LABELS: dict[str, str] = {
    "queued": "首版上画布",
    "feedback_queued": "按修改意见调词/同步",
    "prompt_update_requested": "同步手动修改提示词",
    "recheck_requested": "重新处理与自检",
    "run_requested": "生成 480P 样片",
    "final_requested": "转为 1080P 正片",
    "processing": "后台队列处理",
}

ERROR_DIAGNOSIS_RULES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "network_error",
        ("network issue", "connection", "connect", "econnreset", "timed out", "502", "503", "504", "internal (code 500)", "internal error"),
        "与后台 AI 服务或目标平台（{platform}）的网络连接临时中断（未造成额外扣分，提示词、涂白与已出样片均保留完好）。",
        "① 直接点击下方「↻ 重试刚才操作」重新入队执行；② 若连续两次报相同网络错误，请检查网络/代理连接状态或重启桌面端后再试。",
    ),
    (
        "timeout",
        ("print timeout", "15m0s", "执行超时", "timeout"),
        "本轮后台处理耗时超过单次等待上限（云端生成任务可能仍在排队或其实已出片）。",
        "① 若此前已点过生成，可先点击「↓ 同步成片」拉取最新状态；② 再点击下方「↻ 重试刚才操作」（支持按原任务 ID 续查进度，不会重复扣分）。",
    ),
    (
        "auth_expired",
        ("401", "403", "not logged in", "unauthorized", "login", "auth", "cookie", "token expired", "未登录", "登录", "过期"),
        "{platform} 登录凭证已失效或未完成扫码授权，导致命令行无法读取或写入画布。",
        "① 请点击顶部平台/画布菜单确认并重新登录 {platform}；② 登录成功后点击下方「↻ 重试刚才操作」继续执行。",
    ),
    (
        "credits_insufficient",
        ("insufficient", "credit", "balance", "积分", "余额", "ceiling"),
        "{platform} 账号可用积分不足，或实时报价超出了此前确认的积分上限。",
        "① 请核对 {platform} 账户可用积分余额；② 积分充足后点击下方「↻ 重试刚才操作」或在确认卡片中重新批准最新积分上限。",
    ),
    (
        "moderation",
        ("sensitive", "safety", "policy", "nsfw", "violation", "审核", "违规", "拦截"),
        "提示词描写或参考画面触发了 {platform} 内容安全或写实人脸限制拦截。",
        "① 确认构图参考图是否已完成局部涂白；② 在左侧微调动作/服饰措辞避免敏感词后，点击「✓ 保存并同步画布」再重试。",
    ),
    (
        "asset_missing",
        ("not found", "no such file", "missing", "不存在", "未找到", "404"),
        "本镜头引用的本地原片/参考图文件或云端画布节点已缺失或失效。",
        "请点击顶部「↻ 重新处理·自检」，由 Agent 自动核查并补传缺失素材节点。",
    ),
)


def diagnose_error(
    raw_message: str,
    *,
    failed_task: str = "",
    platform: str = "",
) -> dict[str, str]:
    """Classify raw worker/CLI errors into concise cause + actionable recovery guidance."""
    raw = (raw_message or "").strip()
    lower = raw.lower()
    plat_label = get_platform_label(platform) if platform else "目标平台"
    task_label = TASK_TYPE_LABELS.get((failed_task or "").strip(), "")

    code = "unknown_error"
    reason = ""
    solution = ""
    for rule_code, keywords, reason_tpl, solution_tpl in ERROR_DIAGNOSIS_RULES:
        if any(kw in lower for kw in keywords):
            code = rule_code
            reason = reason_tpl.format(platform=plat_label)
            solution = solution_tpl.format(platform=plat_label)
            break

    if not reason:
        brief = raw or "未知执行异常"
        reason = f"执行过程中遇到异常终止：{brief}。"
        solution = "建议先点击下方「↻ 重试刚才操作」重新执行；若仍失败可点「↻ 重新处理·自检」或「↩ 恢复原状」清除错误状态。"

    if task_label:
        reason = f"在执行「{task_label}」时出错：{reason}"

    return {
        "errorCode": code,
        "errorReason": reason,
        "errorSolution": solution,
        "errorDetail": raw,
    }


def infer_shot_stage_flags(
    shot: dict[str, Any],
    *,
    has_changes: bool | None = None,
) -> dict[str, Any]:
    """Compute consistent UI/API stage flags for 1080P promotion progress and passed state."""
    st = str(shot.get("status") or "")
    task = str(shot.get("processingTaskType") or "")
    jm = shot.get("jimengMeta") or {}
    pending_action = str(jm.get("pendingAction") or "") if isinstance(jm, dict) else ""
    has_final = bool((shot.get("finalVideo") or {}).get("mediaUrl"))
    final_in_progress = bool(
        st == "final_requested"
        or (st == "processing" and task == "final_requested")
        or (st in {"queued", "processing", "needs_confirmation"} and pending_action == "promote_1080p")
    )
    reopened = bool(shot.get("finalModifiedReopened"))
    modified_after_final = bool(
        reopened
        or (
            has_final
            and not final_in_progress
            and st
            in {
                "ready_on_canvas",
                "draft_ready",
                "feedback_queued",
                "prompt_update_requested",
                "run_requested",
                "recheck_requested",
            }
        )
    )
    passed_final = bool(
        not final_in_progress
        and not modified_after_final
        and (
            st in {"final_ready", "final_done"}
            or (has_final and st not in {"error", "needs_confirmation"})
        )
    )
    has_outdated_final = bool(has_final and modified_after_final and not final_in_progress)
    draft_after_reopen = bool(shot.get("draftUpdatedSinceFinalReopen")) or bool(
        has_outdated_final and st == "draft_ready" and (has_changes is False)
    )
    return {
        "hasFinalVideo": has_final,
        "isFinalInProgress": final_in_progress,
        "isPassedFinal": passed_final,
        "hasOutdatedFinal": has_outdated_final,
        "draftUpdatedSinceFinalReopen": draft_after_reopen,
    }


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def sanitize_shot_id(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (raw or "").strip()).strip("_")
    return cleaned.upper() if cleaned else ""


def sanitize_filename(name: str, fallback: str = "file") -> str:
    base = Path(name or fallback).name
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", base).strip("._")
    return cleaned or fallback


def to_media_url(path: Path) -> str:
    rel = path.resolve().relative_to(DATA_ROOT.resolve())
    return "/media/" + rel.as_posix()


def resolve_media_path(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p.resolve()
    return (DATA_ROOT / p).resolve()


def run_preflight(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in preflight_check.IMAGE_EXTS:
        return preflight_check.check_image(path)
    if suffix in preflight_check.VIDEO_EXTS:
        return preflight_check.check_video(path)
    return {"path": str(path), "kind": "unknown", "ok": False, "issues": [f"不支持的格式: {suffix}"]}


def _file_stat_sig(file_path: str | None) -> str:
    if not file_path:
        return ""
    try:
        st = Path(file_path).stat()
        return f"{file_path}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return str(file_path)


def _summarize_shot_assets(shot: dict[str, Any]) -> list[str]:
    summary: list[str] = []
    idx = 1
    comp = shot.get("compImage")
    if comp:
        tag = f"图片{idx} · {comp.get('label') or '构图图'}"
        if comp.get("whiteoutApplied"):
            tag += "(已涂白)"
        summary.append(tag)
        idx += 1
    for cref in shot.get("charRefs") or []:
        tag = f"图片{idx} · {cref.get('label') or '角色图'}"
        if cref.get("whiteoutApplied"):
            tag += "(已涂白)"
        summary.append(tag)
        idx += 1
    src = shot.get("sourceVideo")
    if src:
        summary.append(f"视频{idx} · {src.get('label') or '原片'}")
    return summary


def compute_generation_fingerprint(shot: dict[str, Any]) -> str:
    """Fingerprint inputs that affect LibTV/Jimeng video output for unchanged-rerun detection."""
    plat = normalize_platform(shot.get("platform"))
    payload = {
        "promptText": (shot.get("promptText") or "").strip(),
        "projectUuid": (shot.get("projectUuid") or "").strip(),
        "whiteoutOptions": shot.get("whiteoutOptions") or {},
        "sourceVideo": {
            "sig": _file_stat_sig((shot.get("sourceVideo") or {}).get("filePath")),
            "depthNodeKey": (shot.get("sourceVideo") or {}).get("depthNodeKey") or "",
        },
        "compImage": {
            "sig": _file_stat_sig((shot.get("compImage") or {}).get("filePath")),
            "whiteoutApplied": bool((shot.get("compImage") or {}).get("whiteoutApplied")),
            "whiteoutParts": (shot.get("compImage") or {}).get("whiteoutParts") or [],
        },
        "charRefs": [
            {
                "slotId": c.get("slotId") or "",
                "presetId": c.get("presetId") or "",
                "sig": _file_stat_sig(c.get("filePath")),
                "whiteoutApplied": bool(c.get("whiteoutApplied")),
                "whiteoutParts": c.get("whiteoutParts") or [],
            }
            for c in (shot.get("charRefs") or [])
        ],
    }
    if plat != DEFAULT_PLATFORM:
        payload["platform"] = plat
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _decorate_shot_payload(shot: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(shot)
    plat = normalize_platform(out.get("platform"))
    out["platform"] = plat
    out["platformLabel"] = get_platform_label(plat)
    if not (out.get("canvasUrl") or "").strip():
        out["canvasUrl"] = build_canvas_url(out.get("projectUuid") or "", platform=plat)
    cur_fp = compute_generation_fingerprint(out)
    last_fp = str(out.get("lastGeneratedFingerprint") or "").strip()
    has_video = bool((out.get("draftVideo") or {}).get("mediaUrl") or (out.get("finalVideo") or {}).get("mediaUrl"))
    pending_edit = bool((out.get("pendingPromptEdit") or "").strip())
    st = str(out.get("status") or "")

    if not has_video:
        change_state = "never_generated"
        has_changes = True
        change_summary = "尚未生成过样片"
    elif pending_edit or st in {
        "ready_on_canvas",
        "prompt_update_requested",
        "feedback_queued",
        "recheck_requested",
        "queued",
    } or not last_fp or last_fp != cur_fp:
        change_state = "ready_after_changes"
        has_changes = True
        if pending_edit or st == "prompt_update_requested":
            change_summary = "提示词已修改，等待同步到画布并生成新片"
        elif last_fp and last_fp != cur_fp:
            change_summary = "新版提示词或素材已更新到画布，尚未重新生成新片"
        elif st == "ready_on_canvas":
            change_summary = "新改词/新配置已在画布就绪，尚未生成新版样片"
        else:
            change_summary = "提示词、涂白或素材相对上一版成片有更新"
    else:
        change_state = "unchanged"
        has_changes = False
        change_summary = "提示词与素材同上一版成片一致（无新增改动）"

    out["currentGenerationFingerprint"] = cur_fp
    out["hasChangesSinceLastGeneration"] = has_changes
    out["generationChangeState"] = change_state
    out["generationChangeSummary"] = change_summary
    out.update(infer_shot_stage_flags(out, has_changes=has_changes))
    if st == "error":
        failed_task = str(out.get("lastFailedTask") or "").strip()
        if not failed_task and any(f.get("status") == "pending" for f in (out.get("feedbacks") or [])):
            failed_task = "feedback_queued"
        raw_detail = str(out.get("errorDetail") or out.get("agentSummary") or "").strip()
        diag = diagnose_error(raw_detail, failed_task=failed_task, platform=plat)
        out["errorCode"] = str(out.get("errorCode") or diag["errorCode"])
        out["errorDetail"] = raw_detail or diag["errorDetail"]
        out["errorReason"] = str(out.get("errorReason") or "").strip() or diag["errorReason"]
        out["errorSolution"] = str(out.get("errorSolution") or "").strip() or diag["errorSolution"]
        out["lastFailedTask"] = failed_task
        out["lastFailedTaskLabel"] = TASK_TYPE_LABELS.get(failed_task, "重检同步" if not failed_task else failed_task)
    else:
        out["errorCode"] = ""
        out["errorReason"] = ""
        out["errorSolution"] = ""
        out["errorDetail"] = ""
    return out


def _atomic_write_json(target: Path, payload: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def build_canvas_url(
    project_uuid: str,
    workspace_id: str | int | None = DEFAULT_WORKSPACE_ID,
    platform: str = DEFAULT_PLATFORM,
) -> str:
    plat = normalize_platform(platform)
    uid = (project_uuid or "").strip()
    if plat == "jimeng":
        return f"https://jimeng.jianying.com/ai-tool/ai-canvas/{uid}" if uid else JIMENG_HOME_URL
    if not uid:
        return "https://www.liblib.tv/"
    url = f"https://www.liblib.tv/canvas?projectId={uid}"
    if workspace_id:
        url += f"&spaceId={workspace_id}"
    return url



class QueueStore:
    """Thread-safe persisted state store for Wash Queue & Review Console."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_seeded()

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.is_file():
            return copy.deepcopy(default)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return copy.deepcopy(default)

    def _load_queue(self) -> dict[str, Any]:
        return self._read_json(
            QUEUE_PATH,
            {
                "version": 1,
                "defaultProjectUuid": DEFAULT_PROJECT_UUID,
                "defaultProjectName": DEFAULT_PROJECT_NAME,
                "eventSeq": 1,
                "events": [],
                "shots": [],
            },
        )

    def _save_queue(self, data: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
        data["eventSeq"] = int(data.get("eventSeq", 0)) + 1
        if event is not None:
            event["seq"] = data["eventSeq"]
            event["at"] = now_iso()
            events = list(data.get("events", []))
            events.append(event)
            data["events"] = events[-80:]
        _atomic_write_json(QUEUE_PATH, data)
        self._cv.notify_all()
        return data

    def _load_presets(self) -> dict[str, Any]:
        return self._read_json(PRESETS_PATH, {"version": 1, "presets": []})

    def _save_presets(self, data: dict[str, Any]) -> dict[str, Any]:
        _atomic_write_json(PRESETS_PATH, data)
        queue_data = self._load_queue()
        self._save_queue(queue_data, {"type": "presets_updated"})
        return data

    @staticmethod
    def _summarize_attachments(attachments: list[dict[str, Any]] | None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for att in attachments or []:
            if not isinstance(att, dict):
                continue
            out.append(
                {
                    "name": str(att.get("name") or Path(str(att.get("filePath") or "")).name or "附件"),
                    "mediaUrl": str(att.get("mediaUrl") or ""),
                    "filePath": str(att.get("filePath") or ""),
                }
            )
        return out

    @classmethod
    def _append_version_snapshot(
        cls,
        shot: dict[str, Any],
        *,
        kind: str,
        actor: str,
        title: str,
        prompt_text: str | None = None,
        previous_prompt_text: str = "",
        user_feedback: str = "",
        feedback_tags: list[str] | None = None,
        target_slot: str = "",
        attachments: list[dict[str, Any]] | None = None,
        revision_direction: str = "",
        draft_video_url: str = "",
        draft_downloaded_at: str = "",
        conversation_id: str = "",
        rollback_from_version: str = "",
        created_at: str = "",
        prompt_recorded: bool | None = None,
    ) -> dict[str, Any]:
        history = shot.setdefault("versionHistory", [])
        next_seq = len(history) + 1
        eff_prompt = str(shot.get("promptText") or "" if prompt_text is None else prompt_text).strip()
        recorded = bool(eff_prompt) if prompt_recorded is None else bool(prompt_recorded and eff_prompt)
        entry = {
            "id": f"v{next_seq}_{uuid.uuid4().hex[:6]}",
            "seq": next_seq,
            "version": f"v{next_seq}",
            "kind": kind,
            "actor": actor,
            "title": title.strip(),
            "userFeedback": (user_feedback or "").strip(),
            "feedbackTags": [str(t).strip() for t in (feedback_tags or []) if str(t).strip()],
            "targetSlot": (target_slot or "").strip(),
            "attachments": cls._summarize_attachments(attachments),
            "revisionDirection": (revision_direction or "").strip(),
            "promptText": eff_prompt if recorded else "",
            "previousPromptText": (previous_prompt_text or "").strip(),
            "promptRecorded": recorded,
            "assetSummary": _summarize_shot_assets(shot),
            "status": str(shot.get("status") or ""),
            "draftVideoUrl": (draft_video_url or "").strip(),
            "draftDownloadedAt": (draft_downloaded_at or "").strip(),
            "conversationId": (conversation_id or "").strip(),
            "rollbackFromVersion": (rollback_from_version or "").strip(),
            "createdAt": (created_at or now_iso()).strip(),
        }
        history.append(entry)
        if len(history) > 80:
            del history[:-80]
        return entry

    def _resolve_image_slot(
        self,
        shot: dict[str, Any],
        slot_tag: str = "",
        slot_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        ordered_slots: list[tuple[str, dict[str, Any]]] = []
        if isinstance(shot.get("compImage"), dict):
            ordered_slots.append(("图片1", shot["compImage"]))
        start_idx = 2 if isinstance(shot.get("compImage"), dict) else 1
        for idx, cref in enumerate(shot.get("charRefs", []), start=start_idx):
            ordered_slots.append((f"图片{idx}", cref))
        for tag, obj in ordered_slots:
            if (slot_tag and tag == slot_tag) or (slot_id and obj.get("slotId") == slot_id):
                return tag, obj
        raise ValueError(f"找不到要更新涂白图的素材槽位: {slot_tag or slot_id}")

    def _apply_whiteout_file_to_slot(
        self,
        shot: dict[str, Any],
        *,
        slot_tag: str = "",
        slot_id: str = "",
        file_path: str | Path,
        parts: list[str] | None = None,
        label: str = "",
        libtv_node_key: str | None = None,
    ) -> tuple[str, bool]:
        resolved = resolve_media_path(str(file_path))
        if not resolved.is_file() or DATA_ROOT.resolve() not in resolved.parents:
            raise ValueError(f"涂白结果文件路径无效或不在数据目录内: {file_path}")
        matched_tag, target_obj = self._resolve_image_slot(shot, slot_tag=slot_tag, slot_id=slot_id)
        try:
            ver_token = str(resolved.stat().st_mtime_ns)
        except OSError:
            ver_token = str(int(time.time() * 1000))

        cur_fp = str(target_obj.get("filePath") or "")
        already_same = (
            cur_fp == str(resolved)
            and bool(target_obj.get("whiteoutApplied"))
            and str(target_obj.get("whiteoutVersion") or "") == ver_token
        )
        if already_same:
            return matched_tag, False

        if not target_obj.get("originalFilePath"):
            target_obj["originalFilePath"] = cur_fp
            target_obj["originalMediaUrl"] = target_obj.get("mediaUrl", "")
        target_obj["filePath"] = str(resolved)
        target_obj["mediaUrl"] = to_media_url(resolved)
        target_obj["meta"] = run_preflight(resolved)
        target_obj["whiteoutApplied"] = True
        target_obj["whiteoutVersion"] = ver_token
        clean_parts = [str(x).strip() for x in (parts or []) if str(x).strip()]
        if not clean_parts:
            clean_parts = [
                str(x).strip()
                for x in (target_obj.get("whiteoutParts") or (shot.get("whiteoutOptions") or {}).get("parts") or ["自动涂白"])
                if str(x).strip()
            ]
        target_obj["whiteoutParts"] = clean_parts
        if label:
            target_obj["label"] = label.strip()
        if libtv_node_key is not None:
            target_obj["libtvNodeKey"] = str(libtv_node_key).strip()
        return matched_tag, True

    @staticmethod
    def _find_comp_masked_candidate(
        shot: dict[str, Any],
        *,
        jm_payload: dict[str, Any] | None = None,
        require_unmasked: bool = False,
    ) -> Path | None:
        comp = shot.get("compImage")
        if not isinstance(comp, dict):
            return None
        sid = str(shot.get("id") or "")
        if not sid:
            return None

        explicit_path = str((jm_payload or {}).get("maskedCompPath") or "").strip()
        if not explicit_path and require_unmasked:
            explicit_path = str((shot.get("jimengMeta") or {}).get("maskedCompPath") or "").strip()

        cand: Path | None = None
        if explicit_path:
            p = resolve_media_path(explicit_path)
            if p.is_file() and DATA_ROOT.resolve() in p.parents:
                cand = p
        else:
            default_cand = SHOTS_DIR / sid / "comp_masked.png"
            if default_cand.is_file():
                cand = default_cand
        if cand is None:
            return None

        if require_unmasked:
            if comp.get("originalFilePath") or comp.get("whiteoutApplied"):
                return None
            masks_dir = SHOTS_DIR / sid / "masks"
            if masks_dir.is_dir() and any(masks_dir.glob("manual_图片1_*")):
                return None
            return cand

        # Avoid overriding a newer user manual paint/reset unless explicit maskedCompPath was just supplied
        if not explicit_path:
            cur_fp = str(comp.get("filePath") or "")
            if cur_fp and "manual_图片1_" in Path(cur_fp).name and Path(cur_fp).is_file():
                try:
                    if Path(cur_fp).stat().st_mtime_ns >= cand.stat().st_mtime_ns:
                        return None
                except OSError:
                    return None
        return cand

    def _backfill_shot_tracking(self, shot: dict[str, Any]) -> bool:
        changed = False
        cand_mask = self._find_comp_masked_candidate(shot, require_unmasked=True)
        if cand_mask is not None:
            _, mask_changed = self._apply_whiteout_file_to_slot(
                shot,
                slot_tag="图片1",
                file_path=cand_mask,
                parts=list((shot.get("whiteoutOptions") or {}).get("parts") or []),
            )
            if mask_changed:
                changed = True
        draft_obj = shot.get("draftVideo") or {}
        final_obj = shot.get("finalVideo") or {}
        has_video = bool(draft_obj.get("mediaUrl") or final_obj.get("mediaUrl"))
        cur_status = str(shot.get("status") or "")
        pending_edit = bool((shot.get("pendingPromptEdit") or "").strip())

        if (
            has_video
            and not shot.get("lastGeneratedFingerprint")
            and not pending_edit
            and cur_status in {"draft_ready", "final_ready", "final_done"}
        ):
            shot["lastGeneratedFingerprint"] = compute_generation_fingerprint(shot)
            shot["lastGeneratedAt"] = str(
                draft_obj.get("downloadedAt") or final_obj.get("downloadedAt") or shot.get("updatedAt") or now_iso()
            )
            changed = True

        if not shot.get("versionHistory"):
            feedbacks = list(shot.get("feedbacks") or [])
            cur_prompt = (shot.get("promptText") or "").strip()
            user_prompt = (shot.get("userPrompt") or "").strip()
            wo = shot.get("whiteoutOptions") or {}
            init_prompt = user_prompt if feedbacks else (cur_prompt or user_prompt)
            self._append_version_snapshot(
                shot,
                kind="initial_submit",
                actor="user",
                title="首版提审" if not feedbacks else "首版原始输入",
                prompt_text=init_prompt,
                user_feedback=user_prompt or (shot.get("notes") or "").strip(),
                feedback_tags=list(wo.get("parts") or []),
                target_slot=str(wo.get("targetSlot") or ""),
                revision_direction=(
                    "首版基于原片与用户描述梳理上画布"
                    if not feedbacks
                    else "首版提审记录（后续又经历多轮对话修改）"
                ),
                draft_video_url="" if feedbacks else str(draft_obj.get("mediaUrl") or ""),
                draft_downloaded_at="" if feedbacks else str(draft_obj.get("downloadedAt") or ""),
                created_at=str(shot.get("createdAt") or now_iso()),
                prompt_recorded=bool(init_prompt),
            )

            recorded_current = (not feedbacks and cur_prompt == init_prompt)
            for idx, fb in enumerate(feedbacks):
                is_last = idx == len(feedbacks) - 1
                fb_resolved = fb.get("status") == "resolved"
                res_video = fb.get("resultVideo") or {}
                fb_prompt = (fb.get("editedPrompt") or "").strip()
                if not fb_prompt and is_last and fb_resolved and cur_prompt and not pending_edit:
                    fb_prompt = cur_prompt
                    recorded_current = True
                fb_dir = (fb.get("revisionDirection") or "").strip()
                if not fb_dir:
                    fb_dir = (
                        "历史已执行修改（该早先轮次建立于修改方向字段上线前）"
                        if fb_resolved
                        else "待 Agent 理解并同步本轮修改"
                    )
                self._append_version_snapshot(
                    shot,
                    kind="feedback_resolved" if fb_resolved else "feedback_submitted",
                    actor="agent" if fb_resolved else "user",
                    title=(
                        f"第 {idx + 1} 轮修改已落画布"
                        if fb_resolved
                        else f"第 {idx + 1} 轮待处理修改意见"
                    ),
                    prompt_text=fb_prompt,
                    user_feedback=str(fb.get("comment") or ""),
                    feedback_tags=list(fb.get("tags") or []),
                    target_slot=str(fb.get("targetSlot") or ""),
                    attachments=list(fb.get("attachments") or []),
                    revision_direction=fb_dir,
                    draft_video_url=str(res_video.get("mediaUrl") or ""),
                    draft_downloaded_at=str(res_video.get("downloadedAt") or ""),
                    created_at=str(fb.get("resolvedAt") or fb.get("createdAt") or shot.get("updatedAt") or now_iso()),
                    prompt_recorded=bool(fb_prompt),
                )

            if cur_prompt and not recorded_current:
                self._append_version_snapshot(
                    shot,
                    kind="backfill_current",
                    actor="agent",
                    title="当前画布/最新版提示词快照",
                    prompt_text=cur_prompt,
                    revision_direction=str(shot.get("latestRevisionDirection") or shot.get("agentSummary") or "").strip(),
                    draft_video_url=str(draft_obj.get("mediaUrl") or "") if shot.get("lastGeneratedFingerprint") else "",
                    draft_downloaded_at=str(draft_obj.get("downloadedAt") or "") if shot.get("lastGeneratedFingerprint") else "",
                    created_at=str(shot.get("updatedAt") or now_iso()),
                    prompt_recorded=True,
                )
            changed = True

        if "latestRevisionDirection" not in shot:
            last_dir = ""
            for entry in reversed(shot.get("versionHistory", [])):
                if entry.get("revisionDirection"):
                    last_dir = entry["revisionDirection"]
                    break
            shot["latestRevisionDirection"] = last_dir
            changed = True

        return changed

    def _ensure_seeded(self) -> None:
        with self._lock:
            if not PRESETS_PATH.is_file():
                self._seed_presets()
            else:
                pdata = self._load_presets()
                p_changed = False
                for p in pdata.get("presets", []):
                    if not p.get("scene"):
                        p["scene"] = "通用"
                        p_changed = True
                if p_changed:
                    _atomic_write_json(PRESETS_PATH, pdata)
            if not QUEUE_PATH.is_file():
                self._seed_queue()
            else:
                q = self._load_queue()
                changed = False
                for s in q.get("shots", []):
                    if s.get("id") == "S01" and not s.get("promptText"):
                        extracted = _extract_s01_prompt()
                        if extracted:
                            s["promptText"] = extracted
                            changed = True
                    if isinstance(s.get("canvasUrl"), str) and "libtv.art/canvas/" in s["canvasUrl"]:
                        s["canvasUrl"] = build_canvas_url(s.get("projectUuid") or DEFAULT_PROJECT_UUID)
                        changed = True
                    if "scene" not in s:
                        s["scene"] = "测试场" if s.get("id") == "S01" else ""
                        changed = True
                    if self._backfill_shot_tracking(s):
                        changed = True
                if changed:
                    _atomic_write_json(QUEUE_PATH, q)

    def _copy_if_exists(self, src: Path, dst: Path) -> Path | None:
        if not src.is_file():
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)
        return dst

    def _seed_presets(self) -> None:
        _atomic_write_json(PRESETS_PATH, {"version": 1, "presets": []})


    def _seed_queue(self) -> None:
        _atomic_write_json(
            QUEUE_PATH,
            {
                "version": 1,
                "defaultPlatform": DEFAULT_PLATFORM,
                "platformDefaults": self._default_platform_map(),
                "defaultProjectUuid": "",
                "defaultProjectName": "未选择画布",
                "defaultWorkspaceId": "",
                "eventSeq": 1,
                "events": [],
                "shots": [],
            },
        )


    def _ensure_platform_defaults(self, q: dict[str, Any]) -> dict[str, dict[str, Any]]:
        default_plat = normalize_platform(q.get("defaultPlatform"))
        q["defaultPlatform"] = default_plat
        pdefs = q.get("platformDefaults")
        if not isinstance(pdefs, dict):
            pdefs = {}

        libtv_raw = pdefs.get("libtv") if isinstance(pdefs.get("libtv"), dict) else {}
        libtv_uuid = str(
            libtv_raw.get("projectUuid")
            or (q.get("defaultProjectUuid") if default_plat == "libtv" else "")
            or DEFAULT_PROJECT_UUID
        ).strip()
        libtv_name = str(
            libtv_raw.get("projectName")
            or (q.get("defaultProjectName") if default_plat == "libtv" else "")
            or DEFAULT_PROJECT_NAME
        ).strip()
        libtv_ws = str(
            libtv_raw.get("workspaceId")
            or (q.get("defaultWorkspaceId") if default_plat == "libtv" else "")
            or DEFAULT_WORKSPACE_ID
        ).strip()
        pdefs["libtv"] = {
            "projectUuid": libtv_uuid,
            "projectName": libtv_name,
            "workspaceId": libtv_ws,
            "canvasUrl": str(libtv_raw.get("canvasUrl") or build_canvas_url(libtv_uuid, libtv_ws, "libtv")).strip(),
        }

        jimeng_raw = pdefs.get("jimeng") if isinstance(pdefs.get("jimeng"), dict) else {}
        jimeng_uuid = str(
            jimeng_raw.get("projectUuid")
            or (q.get("defaultProjectUuid") if default_plat == "jimeng" else "")
            or ""
        ).strip()
        jimeng_name = str(
            jimeng_raw.get("projectName")
            or (q.get("defaultProjectName") if default_plat == "jimeng" else "")
            or "未选择即梦画布"
        ).strip()
        jimeng_ws = str(
            jimeng_raw.get("workspaceId")
            or (q.get("defaultWorkspaceId") if default_plat == "jimeng" else "")
            or ""
        ).strip()
        pdefs["jimeng"] = {
            "projectUuid": jimeng_uuid,
            "projectName": jimeng_name,
            "workspaceId": jimeng_ws,
            "canvasUrl": str(jimeng_raw.get("canvasUrl") or build_canvas_url(jimeng_uuid, jimeng_ws, "jimeng")).strip(),
        }
        q["platformDefaults"] = pdefs
        return pdefs

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            q = self._load_queue()
            p = self._load_presets()
            pdefs = self._ensure_platform_defaults(q)
            default_plat = q["defaultPlatform"]
            active_def = pdefs[default_plat]
            return {
                "workspaceRoot": str(WORKSPACE_ROOT),
                "dataRoot": str(DATA_ROOT),
                "queuePath": str(QUEUE_PATH),
                "defaultPlatform": default_plat,
                "platformDefaults": pdefs,
                "defaultProjectUuid": q.get("defaultProjectUuid") or active_def.get("projectUuid", DEFAULT_PROJECT_UUID),
                "defaultProjectName": q.get("defaultProjectName") or active_def.get("projectName", DEFAULT_PROJECT_NAME),
                "defaultWorkspaceId": q.get("defaultWorkspaceId", active_def.get("workspaceId", DEFAULT_WORKSPACE_ID)),
                "eventSeq": q.get("eventSeq", 1),
                "events": q.get("events", [])[-20:],
                "scenes": q.get("scenes", []),
                "presets": p.get("presets", []),
                "shots": [_decorate_shot_payload(s) for s in q.get("shots", [])],
            }

    def create_scene(self, name: str) -> dict[str, Any]:
        clean = (name or "").strip()
        if not clean or clean == "未分场":
            raise ValueError("场号不能为空，且不能命名为“未分场”")
        with self._lock:
            q = self._load_queue()
            scenes = list(q.get("scenes", []))
            if clean not in scenes:
                scenes.append(clean)
                q["scenes"] = scenes
                self._save_queue(q, {"type": "scene_created", "scene": clean})
            return {"scene": clean, "scenes": q.get("scenes", [])}

    def rename_scene(self, old_name: str, new_name: str) -> dict[str, Any]:
        old_clean = (old_name or "").strip()
        new_clean = (new_name or "").strip()
        if not new_clean or new_clean in {"未分场", "通用"}:
            raise ValueError("新场号不能为空，且不能命名为“未分场”或“通用”")
        if old_clean == new_clean:
            return {"oldScene": old_clean, "newScene": new_clean, "updatedCount": 0, "presetUpdatedCount": 0}
        with self._lock:
            q = self._load_queue()
            scenes = list(q.get("scenes", []))
            updated_count = 0
            target_old = "" if old_clean == "未分场" else old_clean
            for shot in q.get("shots", []):
                cur_scene = (shot.get("scene") or "").strip()
                if cur_scene == target_old:
                    shot["scene"] = new_clean
                    shot["updatedAt"] = now_iso()
                    shot.setdefault("revisions", []).append(
                        {
                            "at": now_iso(),
                            "actor": "user",
                            "summary": f"场号更名：{old_clean or '未分场'} → {new_clean}",
                        }
                    )
                    updated_count += 1

            preset_updated_count = 0
            if target_old and target_old != "通用":
                pdata = self._load_presets()
                for preset in pdata.get("presets", []):
                    if (preset.get("scene") or "").strip() == target_old:
                        preset["scene"] = new_clean
                        preset_updated_count += 1
                if preset_updated_count > 0:
                    _atomic_write_json(PRESETS_PATH, pdata)

            new_scenes: list[str] = []
            replaced = False
            for sc in scenes:
                val = new_clean if (target_old and sc == target_old) else sc
                if val == new_clean:
                    replaced = True
                if val and val not in new_scenes:
                    new_scenes.append(val)
            if not replaced and new_clean not in new_scenes:
                new_scenes.append(new_clean)
            q["scenes"] = new_scenes

            self._save_queue(
                q,
                {
                    "type": "scene_renamed",
                    "oldScene": old_clean,
                    "newScene": new_clean,
                    "updatedCount": updated_count,
                    "presetUpdatedCount": preset_updated_count,
                },
            )
            return {
                "oldScene": old_clean,
                "newScene": new_clean,
                "updatedCount": updated_count,
                "presetUpdatedCount": preset_updated_count,
            }

    def delete_scene(self, name: str) -> dict[str, Any]:
        clean = (name or "").strip()
        if not clean or clean in {"未分场", "通用"}:
            raise ValueError("「未分场」或「通用」为系统默认分组，不可删除")
        with self._lock:
            q = self._load_queue()
            scenes = list(q.get("scenes", []))
            existed = clean in scenes
            new_scenes = [sc for sc in scenes if sc != clean]
            moved_count = 0
            for shot in q.get("shots", []):
                if (shot.get("scene") or "").strip() == clean:
                    shot["scene"] = ""
                    shot["updatedAt"] = now_iso()
                    shot.setdefault("revisions", []).append(
                        {
                            "at": now_iso(),
                            "actor": "user",
                            "summary": f"删除场号「{clean}」，镜头移回未分场",
                        }
                    )
                    moved_count += 1

            pdata = self._load_presets()
            preset_moved_count = 0
            for preset in pdata.get("presets", []):
                if (preset.get("scene") or "").strip() == clean:
                    preset["scene"] = "通用"
                    preset_moved_count += 1

            if not existed and moved_count == 0 and preset_moved_count == 0:
                raise KeyError(f"场次不存在: {clean}")

            if preset_moved_count > 0:
                _atomic_write_json(PRESETS_PATH, pdata)

            q["scenes"] = new_scenes
            self._save_queue(
                q,
                {
                    "type": "scene_deleted",
                    "scene": clean,
                    "movedCount": moved_count,
                    "presetMovedCount": preset_moved_count,
                },
            )
            return {
                "scene": clean,
                "movedCount": moved_count,
                "presetMovedCount": preset_moved_count,
                "scenes": q["scenes"],
            }

    def set_default_platform(self, platform: str) -> dict[str, Any]:
        raw = (platform or "").strip().lower()
        if raw not in {"libtv", "jimeng", "dreamina", "即梦", "即梦官网"}:
            raise ValueError("不支持的平台，可选 libtv 或 jimeng")
        plat = normalize_platform(raw)
        with self._lock:
            q = self._load_queue()
            pdefs = self._ensure_platform_defaults(q)
            q["defaultPlatform"] = plat
            active_def = pdefs[plat]
            q["defaultProjectUuid"] = active_def.get("projectUuid", "")
            q["defaultProjectName"] = active_def.get("projectName", "")
            q["defaultWorkspaceId"] = active_def.get("workspaceId", "")
            self._save_queue(q, {"type": "default_platform_changed", "platform": plat})
            return q

    def set_default_project(
        self,
        project_uuid: str,
        project_name: str,
        workspace_id: str | int | None = None,
        platform: str | None = None,
        canvas_url: str = "",
    ) -> dict[str, Any]:
        clean_uuid = project_uuid.strip()
        if not clean_uuid:
            raise ValueError("画布 UUID 不能为空")
        with self._lock:
            q = self._load_queue()
            pdefs = self._ensure_platform_defaults(q)
            plat = normalize_platform(platform or q.get("defaultPlatform"))
            ws_str = ""
            if workspace_id is not None and str(workspace_id).strip() != "":
                ws_str = str(workspace_id).strip()
            elif workspace_id != "":
                ws_str = pdefs[plat].get("workspaceId", "")
            clean_name = project_name.strip() or pdefs[plat].get("projectName", "") or clean_uuid
            eff_url = (canvas_url or "").strip() or build_canvas_url(clean_uuid, ws_str or None, plat)

            pdefs[plat] = {
                "projectUuid": clean_uuid,
                "projectName": clean_name,
                "workspaceId": ws_str,
                "canvasUrl": eff_url,
            }
            if plat == q.get("defaultPlatform", DEFAULT_PLATFORM):
                q["defaultProjectUuid"] = clean_uuid
                q["defaultProjectName"] = clean_name
                q["defaultWorkspaceId"] = ws_str
            self._save_queue(q, {"type": "default_project_changed", "projectUuid": clean_uuid, "platform": plat})
            return q

    def list_presets(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load_presets().get("presets", [])

    def add_preset(
        self,
        name: str,
        role_hint: str,
        filename: str,
        raw_bytes: bytes,
        scene: str = "通用",
        libtv_node_key: str = "",
        project_uuid: str = DEFAULT_PROJECT_UUID,
    ) -> dict[str, Any]:
        with self._lock:
            pid = f"preset_{uuid.uuid4().hex[:8]}"
            ext = Path(filename).suffix.lower() or ".jpg"
            dest = PRESETS_DIR / f"{pid}_{sanitize_filename(name, 'char')}{ext}"
            dest.write_bytes(raw_bytes)
            meta = run_preflight(dest)
            preset = {
                "id": pid,
                "name": name.strip(),
                "scene": scene.strip() or "通用",
                "roleHint": role_hint.strip(),
                "filePath": str(dest),
                "mediaUrl": to_media_url(dest),
                "libtvNodeKeys": {project_uuid: libtv_node_key.strip()} if libtv_node_key.strip() else {},
                "meta": meta,
                "createdAt": now_iso(),
            }
            data = self._load_presets()
            data.setdefault("presets", []).append(preset)
            self._save_presets(data)
            return preset

    def update_preset(self, preset_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._load_presets()
            for preset in data.get("presets", []):
                if preset["id"] == preset_id:
                    if "name" in updates and updates["name"] is not None:
                        preset["name"] = str(updates["name"]).strip()
                    if "scene" in updates and updates["scene"] is not None:
                        preset["scene"] = str(updates["scene"]).strip() or "通用"
                    if "roleHint" in updates and updates["roleHint"] is not None:
                        preset["roleHint"] = str(updates["roleHint"]).strip()
                    if "libtvNodeKey" in updates and "projectUuid" in updates:
                        keys = preset.setdefault("libtvNodeKeys", {})
                        val = str(updates["libtvNodeKey"]).strip()
                        if val:
                            keys[str(updates["projectUuid"])] = val
                        else:
                            keys.pop(str(updates["projectUuid"]), None)
                    self._save_presets(data)
                    return preset
            raise KeyError(f"预设角色不存在: {preset_id}")

    def delete_preset(self, preset_id: str) -> None:
        with self._lock:
            data = self._load_presets()
            before = len(data.get("presets", []))
            remaining = []
            for preset in data.get("presets", []):
                if preset["id"] == preset_id:
                    fp = Path(preset.get("filePath", ""))
                    if fp.is_file() and PRESETS_DIR in fp.parents:
                        fp.unlink(missing_ok=True)
                    continue
                remaining.append(preset)
            if len(remaining) == before:
                raise KeyError(f"预设角色不存在: {preset_id}")
            data["presets"] = remaining
            self._save_presets(data)

    def _resolve_ordered_images(
        self,
        shot_dir: Path,
        project_uuid: str,
        ordered_specs: list[dict[str, Any]],
        uploads: list[tuple[str, bytes]],
        presets: dict[str, Any],
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for idx, spec in enumerate(ordered_specs, start=1):
            kind = spec.get("kind")
            if kind == "preset":
                pid = spec.get("id", "")
                preset = presets.get(pid)
                if not preset:
                    raise ValueError(f"引用的常用角色不存在: {pid}")
                resolved.append(
                    {
                        "slotId": f"{pid}_{idx}",
                        "kind": "preset",
                        "presetId": pid,
                        "label": (spec.get("label") or preset["name"]).strip(),
                        "roleHint": (spec.get("roleHint") or preset.get("roleHint", "")).strip(),
                        "filePath": preset["filePath"],
                        "mediaUrl": preset["mediaUrl"],
                        "libtvNodeKey": preset.get("libtvNodeKeys", {}).get(project_uuid, ""),
                        "meta": preset["meta"],
                    }
                )
            elif kind == "upload":
                up_idx = int(spec["uploadIndex"])
                up_name, up_bytes = uploads[up_idx]
                up_ext = Path(up_name).suffix.lower() or ".jpg"
                default_name = "构图图" if idx == 1 else f"素材{idx}"
                label = (spec.get("label") or Path(up_name).stem or default_name).strip()
                sub = shot_dir / ("comp" + up_ext if idx == 1 else f"chars/img_{idx:02d}_{sanitize_filename(label, 'img')}{up_ext}")
                sub.parent.mkdir(parents=True, exist_ok=True)
                sub.write_bytes(up_bytes)
                resolved.append(
                    {
                        "slotId": "comp" if idx == 1 else f"custom_{idx}_{uuid.uuid4().hex[:6]}",
                        "kind": "custom",
                        "presetId": "",
                        "label": label,
                        "roleHint": (spec.get("roleHint") or "").strip(),
                        "filePath": str(sub),
                        "mediaUrl": to_media_url(sub),
                        "libtvNodeKey": "",
                        "meta": run_preflight(sub),
                    }
                )
        return resolved

    def create_or_replace_shot(
        self,
        shot_id: str,
        project_uuid: str,
        project_name: str,
        notes: str,
        user_prompt: str,
        scene: str = "",
        platform: str = "",
        canvas_url: str = "",
        source_filename: str = "",
        source_bytes: bytes = b"",
        comp_filename: str = "",
        comp_bytes: bytes = b"",
        char_specs: list[dict[str, Any]] | None = None,
        char_uploads: list[tuple[str, bytes]] | None = None,
        ordered_image_specs: list[dict[str, Any]] | None = None,
        whiteout_parts: list[str] | None = None,
        whiteout_target: str = "",
        auto_confirm: bool = True,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        if not sid:
            raise ValueError("镜头编号不能为空（例如 S02）")

        with self._lock:
            q = self._load_queue()
            existing_idx = next((i for i, s in enumerate(q["shots"]) if s["id"] == sid), None)
            if existing_idx is not None and not overwrite:
                raise ValueError(f"镜头 {sid} 已存在，请换个编号或先删除旧镜头")

            shot_dir = SHOTS_DIR / sid
            if shot_dir.exists():
                shutil.rmtree(shot_dir)
            (shot_dir / "chars").mkdir(parents=True, exist_ok=True)
            (shot_dir / "masks").mkdir(parents=True, exist_ok=True)
            (shot_dir / "feedback_assets").mkdir(parents=True, exist_ok=True)

            source_video_obj = None
            if source_bytes:
                src_ext = Path(source_filename).suffix.lower() or ".mp4"
                src_path = shot_dir / f"source{src_ext}"
                src_path.write_bytes(source_bytes)
                source_video_obj = {
                    "label": f"{sid}-原片",
                    "filePath": str(src_path),
                    "mediaUrl": to_media_url(src_path),
                    "libtvNodeKey": "",
                    "depthNodeKey": "",
                    "meta": run_preflight(src_path),
                }

            presets = {p["id"]: p for p in self._load_presets().get("presets", [])}
            uploads_list = list(char_uploads or [])

            if ordered_image_specs is not None:
                all_imgs = self._resolve_ordered_images(shot_dir, project_uuid, ordered_image_specs, uploads_list, presets)
                comp_obj = all_imgs[0] if all_imgs else None
                char_refs = all_imgs[1:]
            else:
                comp_obj = None
                if comp_bytes:
                    comp_ext = Path(comp_filename).suffix.lower() or ".jpg"
                    comp_path = shot_dir / f"comp{comp_ext}"
                    comp_path.write_bytes(comp_bytes)
                    comp_obj = {
                        "label": f"{sid}-构图图",
                        "filePath": str(comp_path),
                        "mediaUrl": to_media_url(comp_path),
                        "libtvNodeKey": "",
                        "meta": run_preflight(comp_path),
                    }
                char_refs = self._resolve_ordered_images(
                    shot_dir, project_uuid, list(char_specs or []), uploads_list, presets
                )

            if not source_video_obj and not comp_obj and not char_refs:
                raise ValueError("请至少拖入原片视频或参考图片")

            clean_whiteout_parts = [str(p).strip() for p in (whiteout_parts or []) if str(p).strip()]
            clean_whiteout_target = whiteout_target.strip()
            rev_note = f"创建镜头 {sid}" + (" 并确认提交队列" if auto_confirm else "（草稿）")
            if clean_whiteout_parts:
                target_label = clean_whiteout_target or "自动识别构图图"
                rev_note += f" · 涂白选项：{'、'.join(clean_whiteout_parts)}（{target_label}）"

            pdefs = self._ensure_platform_defaults(q)
            eff_platform = normalize_platform(platform or q.get("defaultPlatform"))
            pdef = pdefs[eff_platform]
            eff_proj_uuid = (project_uuid or "").strip() or pdef.get("projectUuid", "")
            eff_proj_name = (project_name or "").strip() or pdef.get("projectName", "") or (
                "未选择即梦画布" if eff_platform == "jimeng" else DEFAULT_PROJECT_NAME
            )
            eff_canvas_url = (canvas_url or "").strip() or build_canvas_url(
                eff_proj_uuid, pdef.get("workspaceId") or DEFAULT_WORKSPACE_ID, eff_platform
            )

            status = "queued" if auto_confirm else "drafting"
            shot = {
                "id": sid,
                "scene": scene.strip(),
                "status": status,
                "platform": eff_platform,
                "projectUuid": eff_proj_uuid,
                "projectName": eff_proj_name,
                "canvasUrl": eff_canvas_url,
                "notes": notes.strip(),
                "userPrompt": user_prompt.strip(),
                "pendingPromptEdit": "",
                "whiteoutOptions": {
                    "parts": clean_whiteout_parts,
                    "targetSlot": clean_whiteout_target,
                },
                "sourceVideo": source_video_obj,
                "compImage": comp_obj,
                "charRefs": char_refs,
                "promptText": user_prompt.strip(),
                "promptFilePath": "",
                "libtvGroupKey": "",
                "libtvSampleNodeKey": "",
                "libtvFinalNodeKey": "",
                "libtvMixedOrder": [],
                "jimengMeta": {},
                "draftVideo": None,
                "finalVideo": None,
                "confirmationRequest": None,
                "confirmationHistory": [],
                "feedbacks": [],
                "claimToken": "",
                "claimedBy": "",
                "claimedAt": "",
                "processingTaskType": "",
                "agentSummary": "等待 Agent 领单处理…" if auto_confirm else "草稿已保存，尚未提交给 Agent。",
                "revisions": [
                    {
                        "at": now_iso(),
                        "actor": "user",
                        "summary": rev_note,
                    }
                ],
                "versionHistory": [],
                "latestRevisionDirection": (
                    "初始创建镜头，待 Agent 整理首版提示词并上画布。" if auto_confirm else "草稿保存，尚未提交给 Agent。"
                ),
                "lastGeneratedFingerprint": "",
                "lastGeneratedAt": "",
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
            }
            self._append_version_snapshot(
                shot,
                kind="initial_submit",
                actor="user",
                title="首版提审" if auto_confirm else "创建草稿",
                prompt_text=user_prompt.strip(),
                user_feedback=user_prompt.strip() or notes.strip(),
                feedback_tags=clean_whiteout_parts,
                target_slot=clean_whiteout_target,
                revision_direction=shot["latestRevisionDirection"],
                created_at=shot["createdAt"],
                prompt_recorded=bool(user_prompt.strip()),
            )

            if existing_idx is not None:
                q["shots"][existing_idx] = shot
            else:
                q["shots"].append(shot)

            self._save_queue(q, {"type": "shot_created", "shotId": sid, "status": status})
            return _decorate_shot_payload(shot)

    def get_shot(self, shot_id: str) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] == sid:
                    return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def confirm_shot(self, shot_id: str) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] == sid:
                    self._clear_claim_fields(shot)
                    self._clear_error_fields(shot)
                    shot["status"] = "queued"
                    shot["agentSummary"] = "已确认入队，等待 Agent 开始处理…"
                    shot["updatedAt"] = now_iso()
                    shot.setdefault("revisions", []).append(
                        {"at": now_iso(), "actor": "user", "summary": "点击确认，镜头进入待处理队列"}
                    )
                    self._save_queue(q, {"type": "shot_confirmed", "shotId": sid, "status": "queued"})
                    return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def submit_feedback(
        self,
        shot_id: str,
        comment: str,
        tags: list[str],
        target_slot: str,
        attachments: list[tuple[str, bytes]],
        edited_prompt: str = "",
    ) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                fb_id = f"fb_{uuid.uuid4().hex[:8]}"
                fb_dir = SHOTS_DIR / sid / "feedback_assets" / fb_id
                saved_files: list[dict[str, Any]] = []
                if attachments:
                    fb_dir.mkdir(parents=True, exist_ok=True)
                    for idx, (fname, raw) in enumerate(attachments, start=1):
                        dest = fb_dir / f"{idx:02d}_{sanitize_filename(fname, 'ref')}"
                        dest.write_bytes(raw)
                        saved_files.append(
                            {
                                "name": fname,
                                "filePath": str(dest),
                                "mediaUrl": to_media_url(dest),
                                "meta": run_preflight(dest),
                            }
                        )

                fb = {
                    "id": fb_id,
                    "comment": comment.strip(),
                    "tags": [t for t in tags if t.strip()],
                    "targetSlot": target_slot.strip(),
                    "editedPrompt": edited_prompt.strip(),
                    "attachments": saved_files,
                    "status": "pending",
                    "createdAt": now_iso(),
                    "resolvedAt": "",
                }
                prev_prompt = (shot.get("promptText") or "").strip()
                edited_clean = edited_prompt.strip()
                shot.setdefault("feedbacks", []).append(fb)
                if fb["tags"]:
                    shot["whiteoutOptions"] = {
                        "parts": list(fb["tags"]),
                        "targetSlot": fb["targetSlot"],
                    }
                if edited_clean:
                    if edited_clean != prev_prompt:
                        shot["previousPromptText"] = prev_prompt
                    shot["pendingPromptEdit"] = edited_clean
                    shot["promptText"] = edited_clean
                self._mark_final_reopened_if_needed(shot)
                self._clear_claim_fields(shot)
                self._clear_error_fields(shot)
                shot["status"] = "feedback_queued"
                summary_hint = (
                    fb["comment"][:40]
                    or ("涂白：" + "、".join(fb["tags"]) if fb["tags"] else "")
                    or ("含修改后提示词" if edited_clean else "含附件修改")
                )
                shot["agentSummary"] = f"收到新修改（{summary_hint}），等待 Agent 同步修改并更新画布…"
                shot["updatedAt"] = now_iso()
                rev_summary = fb["comment"] or (
                    f"快捷涂白：{'、'.join(fb['tags'])}" + (f"（{fb['targetSlot']}）" if fb["targetSlot"] else "")
                    if fb["tags"]
                    else ("直接编辑并提交了新版提示词" if edited_clean else "附带新素材")
                )
                shot.setdefault("revisions", []).append(
                    {
                        "at": now_iso(),
                        "actor": "user",
                        "summary": f"提交反馈：{rev_summary}",
                    }
                )
                self._append_version_snapshot(
                    shot,
                    kind="feedback_submitted",
                    actor="user",
                    title=f"提交修改意见（{rev_summary[:24]}）",
                    prompt_text=edited_clean or (shot.get("promptText") or ""),
                    previous_prompt_text=prev_prompt if (edited_clean and edited_clean != prev_prompt) else "",
                    user_feedback=comment.strip(),
                    feedback_tags=fb["tags"],
                    target_slot=fb["targetSlot"],
                    attachments=saved_files,
                    revision_direction="已收到本轮修改意见，等待 Agent 梳理修改方向并同步画布。",
                    prompt_recorded=bool(edited_clean or shot.get("promptText")),
                )
                self._save_queue(
                    q,
                    {"type": "feedback_submitted", "shotId": sid, "feedbackId": fb_id, "status": "feedback_queued"},
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def apply_manual_mask(
        self,
        shot_id: str,
        *,
        slot_tag: str = "",
        slot_id: str = "",
        filename: str = "mask.png",
        raw_bytes: bytes = b"",
        sync_to_canvas: bool = False,
        is_reset: bool = False,
    ) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        if not raw_bytes:
            raise ValueError("涂白画布数据为空")
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                try:
                    matched_tag, target_obj = self._resolve_image_slot(
                        shot, slot_tag=slot_tag, slot_id=slot_id
                    )
                except ValueError as exc:
                    raise ValueError(f"未找到目标图片槽位: {slot_tag or slot_id}") from exc

                if not target_obj.get("originalFilePath"):
                    target_obj["originalFilePath"] = target_obj.get("filePath", "")
                    target_obj["originalMediaUrl"] = target_obj.get("mediaUrl", "")

                masks_dir = SHOTS_DIR / sid / "masks"
                masks_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(filename).suffix.lower() or ".png"
                dest = masks_dir / f"manual_{matched_tag}_{uuid.uuid4().hex[:6]}{ext}"
                dest.write_bytes(raw_bytes)

                if target_obj.get("kind") == "preset":
                    target_obj["kind"] = "custom"
                    target_obj["presetId"] = ""

                target_obj["filePath"] = str(dest)
                target_obj["mediaUrl"] = to_media_url(dest)
                target_obj["meta"] = run_preflight(dest)
                target_obj["whiteoutApplied"] = not is_reset
                target_obj["whiteoutParts"] = [] if is_reset else ["手涂涂白"]
                try:
                    target_obj["whiteoutVersion"] = str(dest.stat().st_mtime_ns)
                except OSError:
                    target_obj["whiteoutVersion"] = str(int(time.time() * 1000))

                action_label = "还原原图" if is_reset else "保存手涂涂白图"
                self._mark_final_reopened_if_needed(shot)
                shot["updatedAt"] = now_iso()
                shot.setdefault("revisions", []).append(
                    {"at": now_iso(), "actor": "user", "summary": f"{action_label}：{matched_tag}"}
                )

                evt_type = "mask_saved"
                if sync_to_canvas:
                    fb_id = f"fb_{uuid.uuid4().hex[:8]}"
                    fb = {
                        "id": fb_id,
                        "comment": f"用户已在画板中完成 {matched_tag} {action_label}，请替换画布对应图片。",
                        "tags": ["还原原图"] if is_reset else ["手涂涂白"],
                        "targetSlot": matched_tag,
                        "editedPrompt": "",
                        "attachments": [
                            {
                                "name": dest.name,
                                "filePath": str(dest),
                                "mediaUrl": target_obj["mediaUrl"],
                                "meta": target_obj["meta"],
                            }
                        ],
                        "status": "pending",
                        "createdAt": now_iso(),
                        "resolvedAt": "",
                    }
                    shot.setdefault("feedbacks", []).append(fb)
                    self._clear_claim_fields(shot)
                    self._clear_error_fields(shot)
                    shot["status"] = "feedback_queued"
                    shot["agentSummary"] = f"收到{action_label}（{matched_tag}），等待 Agent 更新画布…"
                    evt_type = "feedback_submitted"

                self._append_version_snapshot(
                    shot,
                    kind="manual_mask",
                    actor="user",
                    title=f"{action_label}：{matched_tag}",
                    user_feedback=f"画板操作：{matched_tag} {action_label}" + ("（已请求同步画布）" if sync_to_canvas else "（仅本地保留）"),
                    feedback_tags=["还原原图"] if is_reset else ["手涂涂白"],
                    target_slot=matched_tag,
                    attachments=[
                        {
                            "name": dest.name,
                            "filePath": str(dest),
                            "mediaUrl": target_obj["mediaUrl"],
                        }
                    ],
                    revision_direction=f"素材槽位 {matched_tag} 已完成{action_label}。",
                )

                self._save_queue(
                    q,
                    {"type": evt_type, "shotId": sid, "slotTag": matched_tag, "status": shot["status"]},
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def resolve_confirmation(
        self,
        shot_id: str,
        decision: str,
        comment: str = "",
        corrected_file: tuple[str, bytes] | None = None,
    ) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                req = shot.get("confirmationRequest")
                saved_correction = None
                if corrected_file is not None:
                    fname, raw = corrected_file
                    masks_dir = SHOTS_DIR / sid / "masks"
                    masks_dir.mkdir(parents=True, exist_ok=True)
                    dest = masks_dir / f"user_fix_{int(time.time())}_{sanitize_filename(fname, 'mask.png')}"
                    dest.write_bytes(raw)
                    saved_correction = {
                        "filePath": str(dest),
                        "mediaUrl": to_media_url(dest),
                        "meta": run_preflight(dest),
                    }
                record = {
                    "request": req,
                    "decision": decision,
                    "comment": comment.strip(),
                    "correctedAsset": saved_correction,
                    "at": now_iso(),
                }
                shot.setdefault("confirmationHistory", []).append(record)
                shot["confirmationRequest"] = None
                self._clear_claim_fields(shot)
                self._clear_error_fields(shot)
                jm = shot.get("jimengMeta") or {}
                pending_act = str(jm.get("pendingAction") or "") if isinstance(jm, dict) else ""
                if decision == "approve" and pending_act == "promote_1080p":
                    shot["status"] = "final_requested"
                    shot["agentSummary"] = "1080P 正片积分上限已确认，正在提交并等待生成 1080P 正片…"
                elif decision == "approve" and pending_act == "run_sample":
                    jm["pendingAction"] = ""
                    shot["status"] = "run_requested"
                    shot["agentSummary"] = "480P 样片积分上限已确认，正在提交并等待生成 480P 样片…"
                else:
                    if decision != "approve" and isinstance(jm, dict) and pending_act in {"promote_1080p", "run_sample"}:
                        jm["pendingAction"] = ""
                        jm["confirmationState"] = "rejected"
                    shot["status"] = "queued"
                    shot["agentSummary"] = (
                        "人工确认已完成，等待 Agent 继续上传与连线…"
                        if decision == "approve"
                        else "已驳回本次确认请求，等待重新调整…"
                    )
                shot["updatedAt"] = now_iso()
                shot.setdefault("revisions", []).append(
                    {
                        "at": now_iso(),
                        "actor": "user",
                        "summary": f"人工确认({decision})：{comment.strip() or '确认按方案执行'}",
                    }
                )
                self._save_queue(
                    q,
                    {"type": "confirmation_resolved", "shotId": sid, "decision": decision, "status": "queued"},
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def request_run(self, shot_id: str, edited_prompt: str | None = None) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                if edited_prompt is not None and edited_prompt.strip():
                    new_clean = edited_prompt.strip()
                    old_clean = (shot.get("promptText") or "").strip()
                    shot["pendingPromptEdit"] = new_clean
                    shot["promptText"] = new_clean
                    if new_clean != old_clean:
                        shot["previousPromptText"] = old_clean
                        shot["latestRevisionDirection"] = "用户在触发 Run 前直接更新了提示词，将同步新版提示词后生成样片。"
                        self._append_version_snapshot(
                            shot,
                            kind="manual_prompt",
                            actor="user",
                            title="触发生成前手动修改提示词",
                            prompt_text=new_clean,
                            previous_prompt_text=old_clean,
                            revision_direction=shot["latestRevisionDirection"],
                        )
                plat_label = get_platform_label(shot.get("platform"))
                self._mark_final_reopened_if_needed(shot)
                self._clear_claim_fields(shot)
                self._clear_error_fields(shot)
                shot["status"] = "run_requested"
                shot["agentSummary"] = f"已请求 Agent 在 {plat_label} 执行 Run 生成 480P 样片，出片后将自动下载到控制台供看片…"
                shot["updatedAt"] = now_iso()
                shot.setdefault("revisions", []).append(
                    {
                        "at": now_iso(),
                        "actor": "user",
                        "summary": f"点击「重新生成」，请求在 {plat_label} 触发 480P 样片生成并下载回看",
                    }
                )
                self._save_queue(q, {"type": "run_requested", "shotId": sid, "status": "run_requested"})
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def request_prompt_update(self, shot_id: str, prompt_text: str, note: str = "") -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        clean_prompt = (prompt_text or "").strip()
        if not clean_prompt:
            raise ValueError("提示词不能为空")
        clean_note = (note or "").strip()
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                plat_label = get_platform_label(shot.get("platform"))
                prev_prompt = (shot.get("promptText") or shot.get("userPrompt") or "").strip()
                cur_status = str(shot.get("status") or "ready_on_canvas")
                self._mark_final_reopened_if_needed(shot)
                if not shot.get("promptUpdatePrevStatus") and cur_status not in {"prompt_update_requested", "processing"}:
                    shot["promptUpdatePrevStatus"] = self._safe_prompt_update_prev_status(shot, cur_status)
                shot["previousPromptText"] = prev_prompt
                shot["promptText"] = clean_prompt
                shot["pendingPromptEdit"] = clean_prompt
                rev_dir = (
                    f"用户手动调整提示词（{clean_note}），等待同步至 {plat_label} 画布节点。"
                    if clean_note
                    else f"用户手动微调了提示词内容，等待同步更新到 {plat_label} 画布节点。"
                )
                shot["latestRevisionDirection"] = rev_dir
                self._clear_claim_fields(shot)
                self._clear_error_fields(shot)
                shot["status"] = "prompt_update_requested"
                shot["agentSummary"] = f"已保存手动修改后的提示词，等待 Agent 同步更新到 {plat_label} 画布并记录…"
                shot["updatedAt"] = now_iso()
                rev_msg = f"手动更新提示词（{clean_note}），已通知 Agent 同步画布" if clean_note else "手动更新提示词并通知 Agent 同步画布"
                shot.setdefault("revisions", []).append(
                    {
                        "at": now_iso(),
                        "actor": "user",
                        "summary": rev_msg,
                    }
                )
                self._append_version_snapshot(
                    shot,
                    kind="manual_prompt",
                    actor="user",
                    title=f"手动改词并同步画布{(' · ' + clean_note[:16]) if clean_note else ''}",
                    prompt_text=clean_prompt,
                    previous_prompt_text=prev_prompt,
                    user_feedback=clean_note,
                    revision_direction=rev_dir,
                )
                self._save_queue(
                    q,
                    {
                        "type": "prompt_update_requested",
                        "shotId": sid,
                        "status": "prompt_update_requested",
                    },
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def update_shot_project(
        self,
        shot_id: str,
        project_uuid: str,
        project_name: str = "",
        canvas_url: str = "",
        workspace_id: str | int | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        clean_uuid = (project_uuid or "").strip()
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                eff_platform = normalize_platform(platform or shot.get("platform") or q.get("defaultPlatform"))
                if not clean_uuid and eff_platform != "jimeng":
                    raise ValueError("画布 UUID 不能为空")
                clean_name = (project_name or "").strip() or clean_uuid or (
                    "未选择即梦画布" if eff_platform == "jimeng" else DEFAULT_PROJECT_NAME
                )
                eff_url = (canvas_url or "").strip() or build_canvas_url(clean_uuid, workspace_id, eff_platform)
                plat_label = get_platform_label(eff_platform)
                if (
                    eff_platform != normalize_platform(shot.get("platform"))
                    or clean_uuid != (shot.get("projectUuid") or "").strip()
                ):
                    self._mark_final_reopened_if_needed(shot)
                shot["platform"] = eff_platform
                shot["projectUuid"] = clean_uuid
                shot["projectName"] = clean_name
                shot["canvasUrl"] = eff_url
                shot["updatedAt"] = now_iso()
                shot.setdefault("revisions", []).append(
                    {"at": now_iso(), "actor": "user", "summary": f"切换所属{plat_label}画布：{clean_name}"}
                )
                self._save_queue(
                    q,
                    {
                        "type": "shot_project_updated",
                        "shotId": sid,
                        "platform": eff_platform,
                        "projectUuid": clean_uuid,
                        "projectName": clean_name,
                    },
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def request_final(self, shot_id: str) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] == sid:
                    self._clear_claim_fields(shot)
                    self._clear_error_fields(shot)
                    if normalize_platform(shot.get("platform")) == "jimeng":
                        jm = shot.setdefault("jimengMeta", {})
                        if isinstance(jm, dict):
                            jm["pendingAction"] = "promote_1080p"
                    shot["status"] = "final_requested"
                    shot["agentSummary"] = "已请求转为 1080P 正片，等待 Agent 执行并下载归档…"
                    shot["updatedAt"] = now_iso()
                    shot.setdefault("revisions", []).append(
                        {"at": now_iso(), "actor": "user", "summary": "确认满意，请求转 1080P 正片"}
                    )
                    self._save_queue(q, {"type": "final_requested", "shotId": sid, "status": "final_requested"})
                    return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def reprocess_shot(
        self,
        shot_id: str,
        reason: str = "",
        edited_prompt: str | None = None,
    ) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        clean_reason = reason.strip()
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                if edited_prompt is not None and edited_prompt.strip():
                    new_clean = edited_prompt.strip()
                    old_clean = (shot.get("promptText") or "").strip()
                    shot["pendingPromptEdit"] = new_clean
                    shot["promptText"] = new_clean
                    if new_clean != old_clean:
                        shot["previousPromptText"] = old_clean
                        shot["latestRevisionDirection"] = "用户在触发重新处理·自检前更新了提示词，将一并核查并同步画布。"
                        self._append_version_snapshot(
                            shot,
                            kind="manual_prompt",
                            actor="user",
                            title="触发重检前手动修改提示词",
                            prompt_text=new_clean,
                            previous_prompt_text=old_clean,
                            revision_direction=shot["latestRevisionDirection"],
                        )
                self._mark_final_reopened_if_needed(shot)
                self._clear_claim_fields(shot)
                self._clear_error_fields(shot)
                shot["status"] = "recheck_requested"
                shot["agentSummary"] = "已触发一键重新处理与自检，等待 Agent 核查画布连线、素材涂白与提示词规范…"
                shot["updatedAt"] = now_iso()
                summary = f"发起重新处理与自检：{clean_reason}" if clean_reason else "发起一键重新处理与自检"
                shot.setdefault("revisions", []).append(
                    {"at": now_iso(), "actor": "user", "summary": summary}
                )
                self._save_queue(
                    q,
                    {
                        "type": "reprocess_requested",
                        "shotId": sid,
                        "status": "recheck_requested",
                        "reason": clean_reason,
                    },
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def update_shot_scene(self, shot_id: str, scene: str) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        clean_scene = scene.strip()
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                prev_scene = (shot.get("scene") or "").strip()
                shot["scene"] = clean_scene
                shot["updatedAt"] = now_iso()
                scenes = list(q.get("scenes", []))
                for sc in (prev_scene, clean_scene):
                    if sc and sc not in scenes:
                        scenes.append(sc)
                q["scenes"] = scenes
                shot.setdefault("revisions", []).append(
                    {"at": now_iso(), "actor": "user", "summary": f"归类到场号：{clean_scene or '未分场'}"}
                )
                self._save_queue(
                    q,
                    {"type": "shot_scene_updated", "shotId": sid, "scene": clean_scene},
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    @staticmethod
    def _clear_claim_fields(shot: dict[str, Any]) -> None:
        shot["claimToken"] = ""
        shot["claimedBy"] = ""
        shot["claimedAt"] = ""
        shot["processingTaskType"] = ""

    @staticmethod
    def _clear_error_fields(shot: dict[str, Any]) -> None:
        for k in ("errorCode", "errorReason", "errorSolution", "errorDetail", "lastFailedTask"):
            shot.pop(k, None)

    @staticmethod
    def _mark_final_reopened_if_needed(shot: dict[str, Any]) -> None:
        """Unlock a passed 1080P shot when the user explicitly modifies/reruns it while preserving archived finalVideo."""
        has_final = bool((shot.get("finalVideo") or {}).get("mediaUrl"))
        if has_final or str(shot.get("status") or "") in {"final_ready", "final_done"}:
            shot["finalModifiedReopened"] = True
            shot["draftUpdatedSinceFinalReopen"] = False

    @staticmethod
    def _safe_prompt_update_prev_status(shot: dict[str, Any], cur_status: str) -> str:
        if cur_status in {"final_ready", "final_done"} or shot.get("finalModifiedReopened"):
            return "draft_ready" if (shot.get("draftVideo") or {}).get("mediaUrl") else "ready_on_canvas"
        return cur_status

    def recover_from_error(self, shot_id: str, mode: str = "retry") -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        clean_mode = (mode or "retry").strip().lower()
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                if clean_mode == "restore":
                    jm = shot.get("jimengMeta") or {}
                    if isinstance(jm, dict) and jm.get("pendingAction") == "promote_1080p":
                        jm["pendingAction"] = ""
                    if (shot.get("finalVideo") or {}).get("mediaUrl") and not shot.get("finalModifiedReopened"):
                        target_status = "final_ready"
                    elif (shot.get("draftVideo") or {}).get("mediaUrl"):
                        target_status = "draft_ready"
                    elif shot.get("libtvSampleNodeKey") or (isinstance(jm, dict) and jm.get("nodeId")):
                        target_status = "ready_on_canvas"
                    else:
                        target_status = "drafting"
                    summary = "已清除出错标记并恢复至上一次稳定状态。"
                else:
                    cand = str(shot.get("lastFailedTask") or "").strip()
                    jm = shot.get("jimengMeta") or {}
                    if cand not in ACTIONABLE_STATUSES:
                        if isinstance(jm, dict) and jm.get("pendingAction") == "promote_1080p":
                            cand = "final_requested"
                        elif any(f.get("status") == "pending" for f in (shot.get("feedbacks") or [])):
                            cand = "feedback_queued"
                        elif (shot.get("pendingPromptEdit") or "").strip():
                            cand = "prompt_update_requested"
                        elif (shot.get("promptText") or "").strip() and shot.get("promptText") != shot.get("userPrompt"):
                            cand = "recheck_requested"
                        else:
                            cand = "queued"
                    target_status = cand
                    task_label = TASK_TYPE_LABELS.get(target_status, target_status)
                    summary = f"已从出错状态一键重试（重新入队：{task_label}），等待 Agent 继续处理…"

                self._clear_claim_fields(shot)
                self._clear_error_fields(shot)
                shot["status"] = target_status
                shot["agentSummary"] = summary
                shot["updatedAt"] = now_iso()
                shot.setdefault("revisions", []).append(
                    {"at": now_iso(), "actor": "user", "summary": summary}
                )
                self._save_queue(
                    q,
                    {
                        "type": "error_recovered",
                        "shotId": sid,
                        "mode": clean_mode,
                        "status": target_status,
                    },
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    @staticmethod
    def _claim_shot_obj(shot: dict[str, Any], worker_id: str) -> dict[str, Any]:
        task_type = str(shot.get("status") or "queued")
        token = uuid.uuid4().hex[:12]
        clean_worker = (worker_id or "coordinator").strip()[:80] or "coordinator"
        now = now_iso()
        shot["status"] = "processing"
        shot["processingTaskType"] = task_type
        shot["claimToken"] = token
        shot["claimedBy"] = clean_worker
        shot["claimedAt"] = now
        shot["updatedAt"] = now
        shot["agentSummary"] = f"已由并发队列签收处理中（{task_type} / {clean_worker}）…"
        return copy.deepcopy(shot)

    def claim_pending(
        self,
        worker_id: str = "coordinator",
        limit: int = 10,
        shot_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        wanted = {sanitize_shot_id(x) for x in (shot_ids or []) if sanitize_shot_id(x)}
        max_items = max(1, min(int(limit or 10), 50))
        with self._lock:
            q = self._load_queue()
            targets = [
                s
                for s in q["shots"]
                if s.get("status") in ACTIONABLE_STATUSES and (not wanted or s["id"] in wanted)
            ][:max_items]
            digest = self._build_digest(targets)
            claimed = [self._claim_shot_obj(s, worker_id) for s in targets]
            if claimed:
                clean_worker = (worker_id or "coordinator").strip()[:80] or "coordinator"
                self._save_queue(
                    q,
                    {
                        "type": "shots_claimed",
                        "shotIds": [c["id"] for c in claimed],
                        "worker": clean_worker,
                        "count": len(claimed),
                    },
                )
            processing_ids = [s["id"] for s in q["shots"] if s.get("status") == "processing"]
            remaining_pending = sum(1 for s in q["shots"] if s.get("status") in ACTIONABLE_STATUSES)
            return {
                "eventSeq": int(q.get("eventSeq", 1)),
                "defaultProjectUuid": q.get("defaultProjectUuid", DEFAULT_PROJECT_UUID),
                "defaultProjectName": q.get("defaultProjectName", DEFAULT_PROJECT_NAME),
                "defaultWorkspaceId": q.get("defaultWorkspaceId", DEFAULT_WORKSPACE_ID),
                "claimedCount": len(claimed),
                "remainingPendingCount": remaining_pending,
                "processingCount": len(processing_ids),
                "processingShotIds": processing_ids,
                "claimed": claimed,
                "digestMarkdown": digest,
            }

    def claim_shot(self, shot_id: str, worker_id: str = "worker") -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                if shot.get("status") not in ACTIONABLE_STATUSES:
                    raise ClaimConflictError(
                        f"镜头 {sid} 当前状态为 {shot.get('status')}，已被其他并发任务签收或无需领单"
                    )
                claimed_copy = self._claim_shot_obj(shot, worker_id)
                self._save_queue(
                    q,
                    {
                        "type": "shot_claimed",
                        "shotId": sid,
                        "worker": claimed_copy["claimedBy"],
                        "status": "processing",
                    },
                )
                return claimed_copy
        raise KeyError(f"镜头不存在: {shot_id}")

    def agent_update_shot(self, shot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            presets_data = self._load_presets()
            presets_changed = False

            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue

                prev_task = str(shot.get("processingTaskType") or shot.get("status") or "")
                prev_status = str(shot.get("status") or "")
                prev_prompt = (shot.get("promptText") or "").strip()

                req_token = payload.get("claimToken")
                if req_token is not None and str(req_token).strip():
                    expected_token = str(shot.get("claimToken") or "")
                    if str(req_token).strip() != expected_token:
                        raise ClaimConflictError(
                            f"镜头 {sid} 的 claimToken 已失效或被用户新操作覆盖，已阻止写回旧状态"
                        )

                if "status" in payload and payload["status"] is not None:
                    st = str(payload["status"]).strip()
                    if st not in ALLOWED_STATUSES:
                        raise ValueError(f"无效状态: {st}")
                    shot["status"] = st

                if "platform" in payload and payload["platform"] is not None:
                    shot["platform"] = normalize_platform(payload["platform"])
                if "jimengMeta" in payload and isinstance(payload["jimengMeta"], dict):
                    jm = shot.setdefault("jimengMeta", {})
                    if isinstance(jm, dict):
                        jm.update({k: v for k, v in payload["jimengMeta"].items() if v is not None})

                for key in (
                    "projectUuid",
                    "projectName",
                    "canvasUrl",
                    "promptText",
                    "promptFilePath",
                    "libtvGroupKey",
                    "libtvSampleNodeKey",
                    "libtvFinalNodeKey",
                    "agentSummary",
                ):
                    if key in payload and payload[key] is not None:
                        shot[key] = payload[key]

                if "sourceLibtvNodeKey" in payload and payload["sourceLibtvNodeKey"] is not None and isinstance(shot.get("sourceVideo"), dict):
                    shot["sourceVideo"]["libtvNodeKey"] = payload["sourceLibtvNodeKey"]
                if "depthLibtvNodeKey" in payload and payload["depthLibtvNodeKey"] is not None and isinstance(shot.get("sourceVideo"), dict):
                    shot["sourceVideo"]["depthNodeKey"] = payload["depthLibtvNodeKey"]
                if "compLibtvNodeKey" in payload and payload["compLibtvNodeKey"] is not None and isinstance(shot.get("compImage"), dict):
                    shot["compImage"]["libtvNodeKey"] = payload["compLibtvNodeKey"]

                if "charLibtvNodeKeys" in payload and isinstance(payload["charLibtvNodeKeys"], dict):
                    p_uuid = shot.get("projectUuid", DEFAULT_PROJECT_UUID)
                    for slot_id, node_key in payload["charLibtvNodeKeys"].items():
                        for cref in shot.get("charRefs", []):
                            if cref["slotId"] == slot_id or cref.get("presetId") == slot_id:
                                cref["libtvNodeKey"] = node_key
                                if cref.get("presetId"):
                                    for p in presets_data.get("presets", []):
                                        if p["id"] == cref["presetId"]:
                                            p.setdefault("libtvNodeKeys", {})[p_uuid] = node_key
                                            presets_changed = True

                if "libtvMixedOrder" in payload and payload["libtvMixedOrder"] is not None:
                    shot["libtvMixedOrder"] = payload["libtvMixedOrder"]

                updated_whiteout_slots: set[str] = set()
                if "whiteoutUpdates" in payload and isinstance(payload["whiteoutUpdates"], list):
                    for upd in payload["whiteoutUpdates"]:
                        if not isinstance(upd, dict):
                            continue
                        fp = upd.get("filePath")
                        if not fp:
                            continue
                        matched_tag, _ = self._apply_whiteout_file_to_slot(
                            shot,
                            slot_tag=str(upd.get("slotTag") or "").strip(),
                            slot_id=str(upd.get("slotId") or "").strip(),
                            file_path=fp,
                            parts=[str(x).strip() for x in (upd.get("parts") or []) if str(x).strip()],
                            label=str(upd.get("label") or "").strip(),
                            libtv_node_key=upd.get("libtvNodeKey"),
                        )
                        updated_whiteout_slots.add(matched_tag)

                if "图片1" not in updated_whiteout_slots and isinstance(shot.get("compImage"), dict):
                    jm_payload = payload.get("jimengMeta") if isinstance(payload.get("jimengMeta"), dict) else {}
                    target_status = str(shot.get("status") or "")
                    should_autosync_mask = bool(
                        (jm_payload and jm_payload.get("maskedCompPath"))
                        or prev_task in {"queued", "feedback_queued", "recheck_requested"}
                        or target_status in {"ready_on_canvas", "needs_confirmation", "draft_ready", "final_ready"}
                    )
                    if should_autosync_mask:
                        cand_mask = self._find_comp_masked_candidate(
                            shot,
                            jm_payload=jm_payload,
                            require_unmasked=False,
                        )
                        if cand_mask is not None:
                            self._apply_whiteout_file_to_slot(
                                shot,
                                slot_tag="图片1",
                                file_path=cand_mask,
                                parts=list((shot.get("whiteoutOptions") or {}).get("parts") or []),
                            )

                if "confirmationRequest" in payload:
                    req = payload["confirmationRequest"]
                    if isinstance(req, dict):
                        for item in list(req.get("images") or []) + list(req.get("items") or []):
                            fp = item.get("filePath")
                            if fp and not item.get("mediaUrl"):
                                resolved = resolve_media_path(fp)
                                if resolved.is_file() and DATA_ROOT.resolve() in resolved.parents:
                                    item["mediaUrl"] = to_media_url(resolved)
                            if item.get("filePath") and not item.get("whiteoutVersion"):
                                try:
                                    p_item = resolve_media_path(item["filePath"])
                                    if p_item.is_file():
                                        item["whiteoutVersion"] = str(p_item.stat().st_mtime_ns)
                                except OSError:
                                    pass
                    shot["confirmationRequest"] = req

                rev_dir = str(payload.get("revisionDirection") or "").strip()
                conv_id = str(payload.get("conversationId") or "").strip()
                if rev_dir:
                    shot["latestRevisionDirection"] = rev_dir

                updated_result_video = None
                for vid_field in ("draftVideo", "finalVideo"):
                    if vid_field in payload and payload[vid_field] is not None:
                        vobj = dict(payload[vid_field])
                        if vobj.get("filePath") and not vobj.get("mediaUrl"):
                            resolved = resolve_media_path(vobj["filePath"])
                            if resolved.is_file() and DATA_ROOT.resolve() in resolved.parents:
                                vobj["mediaUrl"] = to_media_url(resolved)
                                vobj.setdefault("meta", run_preflight(resolved))
                        vobj.setdefault("downloadedAt", now_iso())
                        shot[vid_field] = vobj
                        updated_result_video = copy.deepcopy(vobj)

                if payload.get("finalVideo") is not None or (
                    shot.get("status") in {"final_ready", "final_done"} and prev_task == "final_requested"
                ):
                    shot["finalModifiedReopened"] = False
                    shot["draftUpdatedSinceFinalReopen"] = False
                elif (
                    shot.get("finalModifiedReopened")
                    or (shot.get("finalVideo") or {}).get("mediaUrl")
                ):
                    if payload.get("draftVideo") is not None or (
                        prev_task == "run_requested" and shot.get("status") == "draft_ready"
                    ):
                        shot["finalModifiedReopened"] = True
                        shot["draftUpdatedSinceFinalReopen"] = True
                    elif (
                        shot.get("finalModifiedReopened")
                        and prev_task != "final_requested"
                        and shot.get("status") in {"final_ready", "final_done"}
                    ):
                        shot["status"] = "draft_ready" if (shot.get("draftVideo") or {}).get("mediaUrl") else "ready_on_canvas"

                resolved_fbs: list[dict[str, Any]] = []
                if payload.get("resolveFeedbacks"):
                    shot["pendingPromptEdit"] = ""
                    for fb in shot.get("feedbacks", []):
                        if fb.get("status") == "pending":
                            fb["status"] = "resolved"
                            fb["resolvedAt"] = now_iso()
                            fb["revisionDirection"] = rev_dir or str(
                                payload.get("revisionSummary") or payload.get("agentSummary") or ""
                            ).strip()
                            if updated_result_video is not None:
                                fb["resultVideo"] = copy.deepcopy(updated_result_video)
                            resolved_fbs.append(fb)

                if shot.get("status") == "error":
                    jm = shot.get("jimengMeta") or {}
                    failed_task = prev_task
                    if failed_task not in ACTIONABLE_STATUSES:
                        if isinstance(jm, dict) and jm.get("pendingAction") == "promote_1080p":
                            failed_task = "final_requested"
                        elif any(f.get("status") == "pending" for f in (shot.get("feedbacks") or [])):
                            failed_task = "feedback_queued"
                        elif (shot.get("pendingPromptEdit") or "").strip():
                            failed_task = "prompt_update_requested"
                        elif prev_status not in {"error", "processing"}:
                            failed_task = prev_status
                    raw_detail = str(
                        payload.get("errorDetail")
                        or payload.get("agentSummary")
                        or payload.get("revisionSummary")
                        or shot.get("agentSummary")
                        or ""
                    ).strip()
                    diag = diagnose_error(raw_detail, failed_task=failed_task, platform=shot.get("platform") or "")
                    shot["lastFailedTask"] = failed_task
                    shot["errorCode"] = diag["errorCode"]
                    shot["errorDetail"] = str(payload.get("errorDetail") or diag["errorDetail"] or raw_detail)
                    shot["errorReason"] = str(payload.get("errorReason") or "").strip() or diag["errorReason"]
                    shot["errorSolution"] = str(payload.get("errorSolution") or "").strip() or diag["errorSolution"]
                    if not (payload.get("agentSummary") or "").strip():
                        shot["agentSummary"] = shot["errorReason"]
                elif shot.get("status") != "processing":
                    self._clear_error_fields(shot)

                if shot.get("status") != "processing":
                    self._clear_claim_fields(shot)
                    shot["promptUpdatePrevStatus"] = ""
                    if shot.get("status") in {"ready_on_canvas", "draft_ready", "final_ready"}:
                        shot["pendingPromptEdit"] = ""
                    if shot.get("status") in {"final_ready", "draft_ready", "ready_on_canvas"}:
                        jm = shot.get("jimengMeta")
                        if isinstance(jm, dict) and jm.get("pendingAction") == "promote_1080p":
                            jm["pendingAction"] = ""

                if payload.get("revisionSummary"):
                    shot.setdefault("revisions", []).append(
                        {"at": now_iso(), "actor": "agent", "summary": str(payload["revisionSummary"])}
                    )

                produced_video = bool(
                    updated_result_video is not None
                    or prev_task in {"run_requested", "final_requested"}
                    or shot.get("status") in {"final_ready", "final_done"}
                )
                if produced_video and shot.get("status") in {"draft_ready", "final_ready", "final_done"}:
                    shot["lastGeneratedFingerprint"] = compute_generation_fingerprint(shot)
                    shot["lastGeneratedAt"] = now_iso()

                new_prompt = (shot.get("promptText") or "").strip()
                prompt_changed = bool(new_prompt and new_prompt != prev_prompt)
                if shot.get("status") != "processing" and (
                    updated_result_video is not None
                    or resolved_fbs
                    or prompt_changed
                    or rev_dir
                    or prev_task
                    in {
                        "queued",
                        "feedback_queued",
                        "recheck_requested",
                        "prompt_update_requested",
                        "run_requested",
                        "final_requested",
                    }
                ):
                    if updated_result_video is not None and payload.get("finalVideo"):
                        snap_kind, snap_title = "run_completed", "1080P 正片生成完成"
                    elif updated_result_video is not None or prev_task == "run_requested":
                        snap_kind, snap_title = "run_completed", "480P 样片生成完成"
                    elif resolved_fbs:
                        snap_kind, snap_title = "feedback_resolved", "Agent 按意见改词并同步画布"
                    elif prev_task == "prompt_update_requested":
                        snap_kind, snap_title = "agent_prompt", "Agent 同步手动提示词到画布"
                    elif prev_task == "recheck_requested":
                        snap_kind, snap_title = "agent_prompt", "Agent 完成自检与画布校准"
                    elif prev_task == "queued":
                        snap_kind, snap_title = "agent_prompt", "Agent 完成首版梳理与上画布"
                    else:
                        snap_kind, snap_title = "agent_prompt", "Agent 更新提示词与画布"

                    fb_comments = "；".join(
                        str(f.get("comment") or "").strip() for f in resolved_fbs if str(f.get("comment") or "").strip()
                    )
                    fb_tags: list[str] = []
                    for f in resolved_fbs:
                        for t in f.get("tags") or []:
                            if t not in fb_tags:
                                fb_tags.append(t)
                    fb_slot = next((str(f.get("targetSlot") or "") for f in resolved_fbs if f.get("targetSlot")), "")
                    fb_atts: list[dict[str, Any]] = []
                    for f in resolved_fbs:
                        fb_atts.extend(f.get("attachments") or [])

                    eff_dir = rev_dir or str(
                        shot.get("latestRevisionDirection")
                        or payload.get("revisionSummary")
                        or payload.get("agentSummary")
                        or ""
                    ).strip()
                    vid_url = ""
                    vid_at = ""
                    if updated_result_video is not None:
                        vid_url = str(updated_result_video.get("mediaUrl") or "")
                        vid_at = str(updated_result_video.get("downloadedAt") or now_iso())

                    self._append_version_snapshot(
                        shot,
                        kind=snap_kind,
                        actor="agent",
                        title=snap_title,
                        prompt_text=new_prompt,
                        previous_prompt_text=prev_prompt if prompt_changed else "",
                        user_feedback=fb_comments,
                        feedback_tags=fb_tags,
                        target_slot=fb_slot,
                        attachments=fb_atts,
                        revision_direction=eff_dir,
                        draft_video_url=vid_url,
                        draft_downloaded_at=vid_at,
                        conversation_id=conv_id,
                    )

                shot["updatedAt"] = now_iso()
                if presets_changed:
                    _atomic_write_json(PRESETS_PATH, presets_data)

                self._save_queue(
                    q,
                    {"type": "agent_updated", "shotId": sid, "status": shot["status"]},
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def update_shot_from_libtv_sync(
        self,
        shot_id: str,
        prompt_text: str | None = None,
        mixed_order: list[dict[str, Any]] | None = None,
        draft_path: Path | None = None,
        remote_draft_url: str = "",
        watermark: bool | None = None,
        node_key: str | None = None,
        resource_id: str = "",
    ) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                plat_label = get_platform_label(shot.get("platform"))
                prev_prompt = (shot.get("promptText") or "").strip()
                if prompt_text:
                    shot["promptText"] = prompt_text
                if mixed_order is not None:
                    shot["libtvMixedOrder"] = mixed_order
                if node_key:
                    shot["libtvSampleNodeKey"] = node_key
                new_media_url = ""
                if draft_path is not None and draft_path.is_file():
                    new_media_url = to_media_url(draft_path)
                    if resource_id:
                        jm = shot.setdefault("jimengMeta", {})
                        if isinstance(jm, dict):
                            jm["downloadedResourceId"] = resource_id
                    shot["draftVideo"] = {
                        "filePath": str(draft_path),
                        "mediaUrl": new_media_url,
                        "remoteUrl": remote_draft_url,
                        "watermark": watermark,
                        "meta": run_preflight(draft_path),
                        "downloadedAt": now_iso(),
                    }
                    if shot["status"] in {"ready_on_canvas", "run_requested", "processing", "queued", "feedback_queued", "final_ready", "final_done"}:
                        self._clear_claim_fields(shot)
                        shot["status"] = "draft_ready"
                        shot["agentSummary"] = f"已从 {plat_label} 下载 480P 样片到本地，可在右侧预览看片或对比原片。"
                    if (shot.get("finalVideo") or {}).get("mediaUrl") or shot.get("finalModifiedReopened"):
                        shot["finalModifiedReopened"] = True
                        shot["draftUpdatedSinceFinalReopen"] = True
                    shot["lastGeneratedFingerprint"] = compute_generation_fingerprint(shot)
                    shot["lastGeneratedAt"] = now_iso()
                    shot.setdefault("revisions", []).append(
                        {"at": now_iso(), "actor": "sync", "summary": f"从 {plat_label} 画布拉取 480P 样片完成"}
                    )
                cur_prompt = (shot.get("promptText") or "").strip()
                last_snap = (shot.get("versionHistory") or [{}])[-1]
                if new_media_url and last_snap.get("draftVideoUrl") != new_media_url:
                    self._append_version_snapshot(
                        shot,
                        kind="run_completed",
                        actor="sync",
                        title=f"从 {plat_label} 同步 480P 样片",
                        prompt_text=cur_prompt,
                        revision_direction=str(shot.get("latestRevisionDirection") or "从画布同步最新样片成片").strip(),
                        draft_video_url=new_media_url,
                        draft_downloaded_at=shot["lastGeneratedAt"],
                    )
                elif prompt_text and prompt_text.strip() != prev_prompt:
                    self._append_version_snapshot(
                        shot,
                        kind="agent_prompt",
                        actor="sync",
                        title="从 LibTV 画布同步提示词",
                        prompt_text=cur_prompt,
                        previous_prompt_text=prev_prompt,
                        revision_direction="从 LibTV 画布节点拉取并对齐当前提示词。",
                    )
                shot["updatedAt"] = now_iso()
                self._save_queue(q, {"type": "libtv_synced", "shotId": sid, "status": shot["status"]})
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def rollback_shot(self, shot_id: str, version_id: str, sync_to_canvas: bool = True) -> dict[str, Any]:
        sid = sanitize_shot_id(shot_id)
        vid = (version_id or "").strip()
        if not vid:
            raise ValueError("请指定要回滚的历史版本 ID")
        with self._lock:
            q = self._load_queue()
            for shot in q["shots"]:
                if shot["id"] != sid:
                    continue
                history = list(shot.get("versionHistory") or [])
                target = next(
                    (item for item in history if item.get("id") == vid or item.get("version") == vid),
                    None,
                )
                if target is None:
                    raise KeyError(f"未找到历史版本: {vid}")
                restored_prompt = str(target.get("promptText") or "").strip()
                if not restored_prompt:
                    raise ValueError("该早先轮次未完整保留提示词正文，请选择带有提示词快照的版本回滚。")

                prev_prompt = (shot.get("promptText") or "").strip()
                cur_status = str(shot.get("status") or "ready_on_canvas")
                target_ver = str(target.get("version") or vid)
                target_title = str(target.get("title") or "").strip()
                target_dir = str(target.get("revisionDirection") or "").strip()

                shot["previousPromptText"] = prev_prompt
                shot["promptText"] = restored_prompt
                rollback_dir = f"已回滚至 {target_ver}（{target_title}）" + (f"：{target_dir}" if target_dir else "")
                shot["latestRevisionDirection"] = rollback_dir
                self._mark_final_reopened_if_needed(shot)

                plat_label = get_platform_label(shot.get("platform"))
                has_canvas_node = bool(shot.get("libtvSampleNodeKey") or shot.get("libtvGroupKey"))
                if sync_to_canvas and has_canvas_node:
                    if not shot.get("promptUpdatePrevStatus") and cur_status not in {
                        "prompt_update_requested",
                        "processing",
                    }:
                        shot["promptUpdatePrevStatus"] = self._safe_prompt_update_prev_status(shot, cur_status)
                    shot["pendingPromptEdit"] = restored_prompt
                    self._clear_claim_fields(shot)
                    self._clear_error_fields(shot)
                    shot["status"] = "prompt_update_requested"
                    shot["agentSummary"] = f"已回滚至 {target_ver} 提示词，等待 Agent 同步回 {plat_label} 画布与交付文档（未触发生成）…"
                    evt_type = "prompt_update_requested"
                else:
                    shot["agentSummary"] = f"已在控制台回滚至 {target_ver} 提示词。"
                    evt_type = "shot_rolled_back"

                shot["updatedAt"] = now_iso()
                shot.setdefault("revisions", []).append(
                    {"at": now_iso(), "actor": "user", "summary": f"版本回滚至 {target_ver}（{target_title}）"}
                )
                self._append_version_snapshot(
                    shot,
                    kind="rollback",
                    actor="user",
                    title=f"回滚至 {target_ver}（{target_title}）",
                    prompt_text=restored_prompt,
                    previous_prompt_text=prev_prompt,
                    user_feedback=f"回滚目标版本：{target_ver}",
                    revision_direction=rollback_dir,
                    rollback_from_version=target_ver,
                )
                self._save_queue(
                    q,
                    {
                        "type": evt_type,
                        "shotId": sid,
                        "rollbackTo": target_ver,
                        "status": shot["status"],
                    },
                )
                return _decorate_shot_payload(shot)
        raise KeyError(f"镜头不存在: {shot_id}")

    def delete_shot(self, shot_id: str) -> None:
        sid = sanitize_shot_id(shot_id)
        with self._lock:
            q = self._load_queue()
            before = len(q["shots"])
            q["shots"] = [s for s in q["shots"] if s["id"] != sid]
            if len(q["shots"]) == before:
                raise KeyError(f"镜头不存在: {shot_id}")
            shot_dir = SHOTS_DIR / sid
            if shot_dir.exists() and SHOTS_DIR in shot_dir.parents:
                shutil.rmtree(shot_dir, ignore_errors=True)
            self._save_queue(q, {"type": "shot_deleted", "shotId": sid})

    def get_pending_for_agent(self) -> dict[str, Any]:
        with self._lock:
            q = self._load_queue()
            pdefs = self._ensure_platform_defaults(q)
            default_plat = q["defaultPlatform"]
            pending = [s for s in q["shots"] if s.get("status") in ACTIONABLE_STATUSES]
            processing_ids = [s["id"] for s in q["shots"] if s.get("status") == "processing"]
            awaiting_user = [s for s in q["shots"] if s.get("status") == "needs_confirmation"]
            return {
                "eventSeq": q.get("eventSeq", 1),
                "defaultPlatform": default_plat,
                "platformDefaults": pdefs,
                "defaultProjectUuid": q.get("defaultProjectUuid", DEFAULT_PROJECT_UUID),
                "defaultProjectName": q.get("defaultProjectName", DEFAULT_PROJECT_NAME),
                "defaultWorkspaceId": q.get("defaultWorkspaceId", DEFAULT_WORKSPACE_ID),
                "pendingCount": len(pending),
                "processingCount": len(processing_ids),
                "processingShotIds": processing_ids,
                "awaitingUserCount": len(awaiting_user),
                "pending": pending,
                "digestMarkdown": self._build_digest(pending),
            }

    def _build_digest(self, pending: list[dict[str, Any]]) -> str:
        if not pending:
            return "当前洗片控制台暂无待 Agent 处理的新提交或新反馈。"
        lines = [
            f"请查看洗片控制台待办（queue.json: {QUEUE_PATH}）：",
        ]
        for s in pending:
            st = s["status"]
            plat = normalize_platform(s.get("platform"))
            plat_label = get_platform_label(plat)
            cli_name = "dreamina-canvas" if plat == "jimeng" else "libtv"
            if st == "feedback_queued":
                fbs = [f for f in s.get("feedbacks", []) if f.get("status") == "pending"]
                latest = fbs[-1] if fbs else {}
                extra = " | 含直接改写提示词" if latest.get("editedPrompt") else ""
                wo_target = f"(@{latest.get('targetSlot')})" if latest.get("targetSlot") else ""
                att_tags = []
                img_n = 0
                vid_n = 0
                for att in latest.get("attachments", []):
                    fp = str(att.get("filePath") or att.get("name") or "")
                    is_vid = bool(re.search(r"\.(mp4|mov|mkv|webm|avi|m4v)$", fp, re.I))
                    if is_vid:
                        vid_n += 1
                        att_tags.append(f"新视频{vid_n}={att.get('filePath') or att.get('name')}")
                    else:
                        img_n += 1
                        att_tags.append(f"新图{img_n}={att.get('filePath') or att.get('name')}")
                att_str = f" | 附件({'; '.join(att_tags)})" if att_tags else ""
                lines.append(
                    f"- {s['id']} [{st}] 平台={plat_label}({cli_name}) | 目标画布={s.get('projectName')}({s.get('projectUuid')}) | 反馈: {latest.get('comment', '')}{extra}{att_str} | 涂白/标签={','.join(latest.get('tags', []))}{wo_target}"
                )
            elif st == "recheck_requested":
                vid_path = (s.get("sourceVideo") or {}).get("filePath", "无")
                sample_key = s.get("libtvSampleNodeKey") or f"{s['id']}-样片"
                group_key = s.get("libtvGroupKey") or "未建组"
                lines.append(
                    f"- {s['id']} [{st}] 平台={plat_label}({cli_name}) | 目标画布={s.get('projectName')}({s.get('projectUuid')}) | group={group_key} | sample={sample_key} | 原片={vid_path} | 用户点击了【重新处理/自检】：请检查中断步骤、核对画布节点/连线顺序、对照 seedance-video-prompts 规范自检提示词并补全同步"
                )
            elif st == "prompt_update_requested":
                sample_key = s.get("libtvSampleNodeKey") or f"{s['id']}-样片"
                group_key = s.get("libtvGroupKey") or "未建组"
                prev_status = s.get("promptUpdatePrevStatus") or ("draft_ready" if s.get("draftVideo") else "ready_on_canvas")
                new_p = (s.get("promptText") or "").strip()
                lines.append(
                    f"- {s['id']} [{st}] 平台={plat_label}({cli_name}) | 目标画布={s.get('projectName')}({s.get('projectUuid')}) | group={group_key} | sample={sample_key} | 恢复目标状态={prev_status} | 用户手动修改了提示词并点击【更新提示词】：请阅读最新 promptText（与 previousPromptText 对比），核对 @图片N/@视频N 连线一致后通过 `{cli_name}` 同步更新画布 Seedance 节点（无需点击 Run），完成后通过 `agent-update` 恢复为 `{prev_status}`（新版提示词前80字: {new_p[:80]}）"
                )
            elif st == "run_requested":
                sample_key = s.get("libtvSampleNodeKey") or f"{s['id']}-样片"
                group_key = s.get("libtvGroupKey") or ""
                edited_note = " | 含未同步修改后提示词" if s.get("pendingPromptEdit") else ""
                run_cmd = f"`dreamina-canvas node run --node-id {sample_key}`" if plat == "jimeng" else "`libtv node --run`"
                lines.append(
                    f"- {s['id']} [{st}] 平台={plat_label}({cli_name}) | 目标画布={s.get('projectName')}({s.get('projectUuid')}) | group={group_key or '未记录'} | sample={sample_key}{edited_note} | 请触发 {run_cmd} 生成 480P 样片、下载到本地并在 `agent-update` 中附上 `draftVideo` 置为 `draft_ready` 供用户看片"
                )
            elif st == "final_requested":
                lines.append(
                    f"- {s['id']} [{st}] 平台={plat_label}({cli_name}) | 请将画布样片节点 {s.get('libtvSampleNodeKey') or s['id'] + '-样片'} 转为 1080P 正片并下载归档"
                )
            else:
                up = s.get("userPrompt") or s.get("notes") or ""
                scene_tag = f"[{s.get('scene')}] " if s.get("scene") else ""
                vid_path = (s.get("sourceVideo") or {}).get("filePath", "无")
                comp_path = (s.get("compImage") or {}).get("filePath", "无")
                wo = s.get("whiteoutOptions") or {}
                wo_parts = wo.get("parts") or []
                wo_str = f" | 涂白={','.join(wo_parts)}({wo.get('targetSlot') or '自动'})" if wo_parts else ""
                lines.append(
                    f"- {s['id']} [{st}] 平台={plat_label}({cli_name}) | {scene_tag}目标画布={s.get('projectName')}({s.get('projectUuid')}) | 原片={vid_path} | 构图={comp_path} | 角色数={len(s.get('charRefs', []))}{wo_str} | 用户提示词: {up[:80]}"
                )
        return "\n".join(lines)

    def wait_for_events(self, since_seq: int, timeout: float = 20.0, only_actionable: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.5, min(timeout, 30.0))
        with self._cv:
            while True:
                q = self._load_queue()
                cur_seq = int(q.get("eventSeq", 1))
                pending = [s for s in q.get("shots", []) if s.get("status") in ACTIONABLE_STATUSES]
                processing_ids = [s["id"] for s in q.get("shots", []) if s.get("status") == "processing"]
                if only_actionable:
                    if pending:
                        return {
                            "timedOut": False,
                            "eventSeq": cur_seq,
                            "pendingCount": len(pending),
                            "processingCount": len(processing_ids),
                            "processingShotIds": processing_ids,
                            "pending": pending,
                            "digestMarkdown": self._build_digest(pending),
                        }
                elif cur_seq > since_seq:
                    recent = [e for e in q.get("events", []) if int(e.get("seq", 0)) > since_seq]
                    return {
                        "timedOut": False,
                        "eventSeq": cur_seq,
                        "pendingCount": len(pending),
                        "processingCount": len(processing_ids),
                        "processingShotIds": processing_ids,
                        "recentEvents": recent,
                        "pending": pending,
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "timedOut": True,
                        "eventSeq": cur_seq,
                        "pendingCount": len(pending),
                        "processingCount": len(processing_ids),
                        "processingShotIds": processing_ids,
                        "pending": pending if only_actionable else [],
                    }
                self._cv.wait(timeout=remaining)
