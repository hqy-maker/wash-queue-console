from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from store import WORKSPACE_ROOT

HOME_DIR = Path.home()
APP_ROOT = Path(__file__).resolve().parent
JIMENG_HOME_URL = "https://jimeng.jianying.com/ai-tool/home"

_AUTH_CACHE_LOCK = threading.Lock()
_AUTH_CACHE: dict[str, Any] = {"fetchedAt": 0.0, "status": None}
_PENDING_LOGIN: dict[str, Any] = {
    "active": False,
    "token": 0,
    "verificationUrl": "",
    "userCode": "",
    "lastError": "",
}


class JimengAuthRequiredError(RuntimeError):
    """Raised when dreamina-canvas requires user OAuth authorization."""


def _find_dreamina_bin() -> str | None:
    candidates = [
        os.environ.get("DREAMINA_BIN", "").strip(),
        str(HOME_DIR / ".local" / "bin" / "dreamina-canvas"),
        str(APP_ROOT / "bin" / "dreamina-canvas"),
        "/opt/homebrew/bin/dreamina-canvas",
        "/usr/local/bin/dreamina-canvas",
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return shutil.which("dreamina-canvas")


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    prefix = f"{HOME_DIR / '.local' / 'bin'}:{HOME_DIR / '.libtv'}:{APP_ROOT / 'bin'}:/opt/homebrew/bin:/usr/local/bin"
    env["PATH"] = f"{prefix}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def _run_dreamina(args: list[str], timeout: int = 35, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    binary = _find_dreamina_bin()
    if not binary:
        raise FileNotFoundError("未找到 dreamina-canvas CLI，请先安装。")
    cmd = [binary, "--non-interactive", "--format", "json", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd or WORKSPACE_ROOT),
        env=_cli_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _extract_json(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"未能解析 dreamina-canvas JSON 输出: {raw[:300]}")
    return json.loads(raw[start : end + 1])


def _extract_error_msg(payload: dict[str, Any] | None, fallback: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            diags = err.get("diagnostics") or []
            if isinstance(diags, list) and diags:
                detail = str((diags[0] or {}).get("detail") or "").strip()
                if detail:
                    return detail
            msg = str(err.get("message") or "").strip()
            if msg:
                return msg
    return fallback


def _is_auth_error(proc: subprocess.CompletedProcess[str], payload: dict[str, Any] | None) -> bool:
    if proc.returncode == 11:
        return True
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "").lower()
            reason = str(err.get("reason") or "").lower()
            if "authentication" in msg or "login" in msg or "unauthorized" in msg or "auth" in reason:
                return True
    return False


def invalidate_auth_cache() -> None:
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE["fetchedAt"] = 0.0
        _AUTH_CACHE["status"] = None


def _pending_snapshot() -> dict[str, Any]:
    with _AUTH_CACHE_LOCK:
        return dict(_PENDING_LOGIN)


def get_auth_status(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with _AUTH_CACHE_LOCK:
        cached = _AUTH_CACHE.get("status")
        if not force and cached is not None and (now - float(_AUTH_CACHE.get("fetchedAt", 0.0)) < 15.0):
            return dict(cached)

    binary = _find_dreamina_bin()
    if not binary:
        res = {
            "installed": False,
            "authenticated": False,
            "status": "missing_cli",
            "accountName": "",
            "credits": None,
            "isVip": False,
            "spaceType": "",
            "teamName": "",
            "loginPending": False,
            "verificationUrl": "",
            "userCode": "",
            "detail": "本机未安装 dreamina-canvas CLI",
        }
        with _AUTH_CACHE_LOCK:
            _AUTH_CACHE["fetchedAt"] = now
            _AUTH_CACHE["status"] = dict(res)
        return res

    pending = _pending_snapshot()
    try:
        proc = _run_dreamina(["auth", "status"], timeout=15)
        payload = _extract_json(proc)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        logged_in = bool(data.get("loggedIn"))
    except Exception as exc:
        return {
            "installed": True,
            "authenticated": False,
            "status": "error",
            "accountName": "",
            "credits": None,
            "isVip": False,
            "spaceType": "",
            "teamName": "",
            "loginPending": bool(pending.get("active")),
            "verificationUrl": str(pending.get("verificationUrl") or ""),
            "userCode": str(pending.get("userCode") or ""),
            "detail": f"检查即梦登录状态失败: {exc}",
        }

    if not logged_in:
        res = {
            "installed": True,
            "authenticated": False,
            "status": "authorization_required" if pending.get("active") else "not_logged_in",
            "accountName": "",
            "credits": None,
            "isVip": False,
            "spaceType": "",
            "teamName": "",
            "loginPending": bool(pending.get("active")),
            "verificationUrl": str(pending.get("verificationUrl") or ""),
            "userCode": str(pending.get("userCode") or ""),
            "detail": (
                "请在打开的即梦网页中确认授权登录…"
                if pending.get("active")
                else (pending.get("lastError") or "即梦官网尚未登录授权，点击下方按钮扫码/确认登录后即可读取画布列表。")
            ),
        }
        with _AUTH_CACHE_LOCK:
            _AUTH_CACHE["fetchedAt"] = now
            _AUTH_CACHE["status"] = dict(res)
        return res

    with _AUTH_CACHE_LOCK:
        _PENDING_LOGIN["active"] = False
        _PENDING_LOGIN["verificationUrl"] = ""
        _PENDING_LOGIN["userCode"] = ""
        _PENDING_LOGIN["lastError"] = ""

    account_name = ""
    credits: Any = None
    is_vip = False
    space_type = "personal"
    team_name = ""

    try:
        acc_proc = _run_dreamina(["auth", "account"], timeout=15)
        if acc_proc.returncode == 0:
            acc_data = (_extract_json(acc_proc).get("data") or {})
            account_name = str(acc_data.get("displayName") or "").strip()
            credits = acc_data.get("availableCredits")
            if credits is None:
                credits = acc_data.get("credits")
            is_vip = bool(acc_data.get("isVip"))
    except Exception:
        pass

    try:
        sp_proc = _run_dreamina(["space", "current"], timeout=15)
        if sp_proc.returncode == 0:
            sp_data = (_extract_json(sp_proc).get("data") or {})
            if not account_name:
                account_name = str(sp_data.get("displayName") or "").strip()
            if credits is None:
                credits = sp_data.get("credits")
            if not is_vip:
                is_vip = bool(sp_data.get("isVip"))
            space_type = str(sp_data.get("kind") or "personal").strip() or "personal"
            team_obj = sp_data.get("team") if isinstance(sp_data.get("team"), dict) else {}
            team_name = str(team_obj.get("name") or "").strip()
    except Exception:
        pass

    res = {
        "installed": True,
        "authenticated": True,
        "status": "logged_in",
        "accountName": account_name or "即梦账号",
        "credits": credits,
        "isVip": is_vip,
        "spaceType": space_type,
        "teamName": team_name,
        "loginPending": False,
        "verificationUrl": "",
        "userCode": "",
        "detail": f"已连接即梦账号：{account_name or '已登录'}" + (f"（团队：{team_name}）" if team_name else ""),
    }
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE["fetchedAt"] = now
        _AUTH_CACHE["status"] = dict(res)
    return res


def _spawn_auth_wait_worker(device_code: str, token: int) -> None:
    def _runner() -> None:
        # Device code stays strictly inside this worker; timeout budget uses error.code 40011.
        for _ in range(3):
            with _AUTH_CACHE_LOCK:
                if not _PENDING_LOGIN["active"] or _PENDING_LOGIN["token"] != token:
                    return
            try:
                proc = _run_dreamina(["auth", "wait", "--device-code", device_code, "--timeout", "60s"], timeout=75)
                payload = _extract_json(proc)
            except Exception as exc:
                with _AUTH_CACHE_LOCK:
                    if _PENDING_LOGIN["token"] == token:
                        _PENDING_LOGIN["active"] = False
                        _PENDING_LOGIN["lastError"] = f"授权等待异常: {exc}"
                invalidate_auth_cache()
                return

            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            if payload.get("ok") and data.get("status") in {"logged_in", "reused"}:
                with _AUTH_CACHE_LOCK:
                    if _PENDING_LOGIN["token"] == token:
                        _PENDING_LOGIN["active"] = False
                        _PENDING_LOGIN["verificationUrl"] = ""
                        _PENDING_LOGIN["userCode"] = ""
                        _PENDING_LOGIN["lastError"] = ""
                invalidate_auth_cache()
                return

            err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = err.get("code")
            if code == 40011:
                continue

            msg = _extract_error_msg(payload, "即梦授权未完成或已过期，请重新点击登录。")
            with _AUTH_CACHE_LOCK:
                if _PENDING_LOGIN["token"] == token:
                    _PENDING_LOGIN["active"] = False
                    _PENDING_LOGIN["lastError"] = msg
            invalidate_auth_cache()
            return

        with _AUTH_CACHE_LOCK:
            if _PENDING_LOGIN["token"] == token:
                _PENDING_LOGIN["active"] = False
                _PENDING_LOGIN["lastError"] = "授权等待超时，请重新发起即梦官网登录。"
        invalidate_auth_cache()

    th = threading.Thread(target=_runner, name="dreamina-auth-wait", daemon=True)
    th.start()


def start_auth_login() -> dict[str, Any]:
    proc = _run_dreamina(["auth", "login"], timeout=25)
    payload = _extract_json(proc)
    if not payload.get("ok") and proc.returncode != 0:
        raise RuntimeError(_extract_error_msg(payload, proc.stderr.strip() or "启动即梦登录失败"))

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    status = str(data.get("status") or "").strip()
    if status in {"logged_in", "reused"} or bool((data.get("state") or {}).get("loggedIn")):
        invalidate_auth_cache()
        with _AUTH_CACHE_LOCK:
            _PENDING_LOGIN["active"] = False
            _PENDING_LOGIN["verificationUrl"] = ""
            _PENDING_LOGIN["userCode"] = ""
            _PENDING_LOGIN["lastError"] = ""
        return {
            "authenticated": True,
            "status": status or "logged_in",
            "verificationUrl": "",
            "userCode": "",
        }

    challenge = data.get("challenge") if isinstance(data.get("challenge"), dict) else {}
    device_code = str(challenge.get("deviceCode") or "").strip()
    user_code = str(challenge.get("userCode") or "").strip()
    verify_url = str(challenge.get("verificationUriComplete") or challenge.get("verificationUri") or "").strip()
    if status == "authorization_required" and device_code and verify_url:
        with _AUTH_CACHE_LOCK:
            _PENDING_LOGIN["token"] = int(_PENDING_LOGIN.get("token", 0)) + 1
            tok = _PENDING_LOGIN["token"]
            _PENDING_LOGIN["active"] = True
            _PENDING_LOGIN["verificationUrl"] = verify_url
            _PENDING_LOGIN["userCode"] = user_code
            _PENDING_LOGIN["lastError"] = ""
        invalidate_auth_cache()
        _spawn_auth_wait_worker(device_code, tok)
        return {
            "authenticated": False,
            "status": "authorization_required",
            "verificationUrl": verify_url,
            "userCode": user_code,
        }

    raise RuntimeError(f"未能获取即梦授权链接（status={status or 'unknown'}）")


def _current_space_label() -> tuple[str, str]:
    try:
        proc = _run_dreamina(["space", "current"], timeout=12)
        if proc.returncode == 0:
            data = _extract_json(proc).get("data") or {}
            if str(data.get("kind") or "") == "team":
                team = data.get("team") if isinstance(data.get("team"), dict) else {}
                return str(team.get("name") or "团队空间"), str(team.get("teamId") or "")
            return "即梦官网", ""
    except Exception:
        pass
    return "即梦官网", ""


def list_projects() -> list[dict[str, Any]]:
    space_label, team_id = _current_space_label()
    projects: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""

    for _ in range(5):
        args = ["canvas", "ls", "--limit", "50"]
        if cursor:
            args.extend(["--cursor", cursor])
        proc = _run_dreamina(args, timeout=25)
        try:
            payload = _extract_json(proc)
        except Exception:
            payload = None

        if proc.returncode != 0 or not (isinstance(payload, dict) and payload.get("ok")):
            if _is_auth_error(proc, payload):
                invalidate_auth_cache()
                raise JimengAuthRequiredError("即梦官网尚未登录授权")
            raise RuntimeError(_extract_error_msg(payload, proc.stderr.strip() or "读取即梦画布列表失败"))

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = data.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("projectId") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            raw_name = str(item.get("name") or pid).strip()
            web_url = str(item.get("webUrl") or "").strip() or f"https://jimeng.jianying.com/ai-tool/ai-canvas/{pid}"
            projects.append(
                {
                    "uuid": pid,
                    "name": raw_name,
                    "rawName": raw_name,
                    "workspaceName": space_label,
                    "workspaceId": team_id,
                    "updatedAtMs": item.get("updatedAt"),
                    "url": web_url,
                    "platform": "jimeng",
                }
            )

        if not data.get("hasMore") or not data.get("nextCursor"):
            break
        cursor = str(data.get("nextCursor"))

    projects.sort(key=lambda x: int(x.get("updatedAtMs") or 0), reverse=True)
    return projects


def create_project(name: str, description: str = "") -> dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("画布名称不能为空")

    proc = _run_dreamina(["canvas", "create", clean_name, "--use"], timeout=25)
    try:
        payload = _extract_json(proc)
    except Exception:
        payload = None

    if proc.returncode != 0 or not (isinstance(payload, dict) and payload.get("ok")):
        if _is_auth_error(proc, payload):
            invalidate_auth_cache()
            raise JimengAuthRequiredError("即梦官网尚未登录授权")
        raise RuntimeError(_extract_error_msg(payload, proc.stderr.strip() or "新建即梦画布失败"))

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    proj = data.get("project") if isinstance(data.get("project"), dict) else {}
    pid = str(proj.get("projectId") or "").strip()
    if not pid:
        raise RuntimeError("已创建即梦画布但未返回 projectId")

    space_label, team_id = _current_space_label()
    raw_name = str(proj.get("name") or clean_name).strip()
    web_url = str(proj.get("webUrl") or "").strip() or f"https://jimeng.jianying.com/ai-tool/ai-canvas/{pid}"
    return {
        "uuid": pid,
        "name": raw_name,
        "rawName": raw_name,
        "workspaceName": space_label,
        "workspaceId": team_id,
        "updatedAtMs": proj.get("updatedAt"),
        "url": web_url,
        "platform": "jimeng",
    }


def download_resource(resource_id: str, dest_file: Path, project_uuid: str | None = None) -> Path:
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jimeng_dl_", dir=str(dest_file.parent)) as tmp_dir:
        tmp_out = Path(tmp_dir) / dest_file.name
        args = ["resource", "download", resource_id, "--output", str(tmp_out)]
        if project_uuid:
            args.extend(["--project-id", project_uuid])
        proc = _run_dreamina(args, timeout=180)
        try:
            payload = _extract_json(proc)
        except Exception:
            payload = None

        if proc.returncode != 0 or not (isinstance(payload, dict) and payload.get("ok")):
            if _is_auth_error(proc, payload):
                invalidate_auth_cache()
                raise JimengAuthRequiredError("即梦官网尚未登录授权")
            raise RuntimeError(_extract_error_msg(payload, proc.stderr.strip() or f"下载即梦资源 {resource_id} 失败"))

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        out_path = Path(str(data.get("path") or tmp_out))
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise RuntimeError(f"下载到的即梦文件为空: {resource_id}")

        tmp_target = dest_file.with_name(f".{dest_file.name}.tmp")
        shutil.copy2(out_path, tmp_target)
        os.replace(tmp_target, dest_file)
        return dest_file


def _inspect_dreamina_node_raw(node_id: str, project_uuid: str) -> dict[str, Any]:
    proc = _run_dreamina(["node", "show", "--node-id", node_id, "--project-id", project_uuid], timeout=25)
    try:
        payload = _extract_json(proc)
    except Exception:
        payload = None

    if proc.returncode != 0 or not (isinstance(payload, dict) and payload.get("ok")):
        if _is_auth_error(proc, payload):
            invalidate_auth_cache()
            raise JimengAuthRequiredError("即梦官网尚未登录授权")
        raise RuntimeError(_extract_error_msg(payload, proc.stderr.strip() or f"读取即梦节点失败: {node_id}"))

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nodes = data.get("nodes") or []
    first_item = nodes[0] if nodes and isinstance(nodes[0], dict) else {}
    node = first_item.get("node") if isinstance(first_item.get("node"), dict) else {}
    if not node:
        raise RuntimeError(f"即梦画布中未找到节点 {node_id}（result={first_item.get('result') or 'unknown'}）")
    return node


def sync_shot_node(shot: dict[str, Any], shot_dir: Path) -> dict[str, Any]:
    """Inspect a Dreamina Canvas node (sample & optional 1080P final) and pull latest completed assets."""
    node_ref = str(shot.get("libtvSampleNodeKey") or "").strip()
    project_uuid = str(shot.get("projectUuid") or "").strip()
    if not node_ref or not project_uuid:
        raise ValueError("当前镜头尚未绑定即梦画布或视频节点 ID，无法拉取节点状态")

    node = _inspect_dreamina_node_raw(node_ref, project_uuid)

    gen = node.get("generation") if isinstance(node.get("generation"), dict) else {}
    resources = [r for r in (node.get("resources") or []) if isinstance(r, dict)]
    node_status = str(node.get("status") or "").lower()

    succeeded = [r for r in resources if str(r.get("status") or "").lower() in {"succeeded", "success", "completed"}]
    chosen_res = succeeded[0] if succeeded else (resources[0] if resources else None)
    resource_id = str((chosen_res or {}).get("resourceId") or "").strip()

    mixed_order = []
    for idx, ref in enumerate(gen.get("references") or [], start=1):
        if not isinstance(ref, dict):
            continue
        mixed_order.append(
            {
                "index": idx,
                "mediaType": str(ref.get("type") or "image"),
                "nodeKey": str(ref.get("id") or ""),
                "label": str(ref.get("title") or ref.get("id") or f"参考{idx}"),
            }
        )

    draft_path: Path | None = None
    download_warning = ""
    jm = shot.get("jimengMeta") or {}
    existing_dl_res = str(jm.get("downloadedResourceId") or "").strip()
    target = shot_dir / "draft_480p.mp4"
    if resource_id and succeeded:
        if not target.is_file() or existing_dl_res != resource_id:
            try:
                draft_path = download_resource(resource_id, target, project_uuid=project_uuid)
            except JimengAuthRequiredError:
                raise
            except Exception as dl_exc:
                download_warning = str(dl_exc)
                if target.is_file():
                    draft_path = target
        else:
            draft_path = target

    final_path: Path | None = None
    final_res_id = ""
    final_node_key = str(jm.get("finalNodeId") or jm.get("upscaleNodeId") or shot.get("libtvFinalNodeKey") or "").strip()
    if final_node_key:
        try:
            fnode = _inspect_dreamina_node_raw(final_node_key, project_uuid)
            fresources = [r for r in (fnode.get("resources") or []) if isinstance(r, dict)]
            fsuc = [r for r in fresources if str(r.get("status") or "").lower() in {"succeeded", "success", "completed"}]
            if fsuc:
                final_res_id = str(fsuc[0].get("resourceId") or "").strip()
            existing_final_res = str(jm.get("downloadedFinalResourceId") or "").strip()
            ftarget = shot_dir / "final_1080p.mp4"
            if final_res_id and fsuc:
                if not ftarget.is_file() or existing_final_res != final_res_id:
                    final_path = download_resource(final_res_id, ftarget, project_uuid=project_uuid)
                else:
                    final_path = ftarget
            final_node_key = str(fnode.get("nodeId") or final_node_key)
        except JimengAuthRequiredError:
            raise
        except Exception as fin_exc:
            if not download_warning:
                download_warning = str(fin_exc)

    return {
        "nodeKey": str(node.get("nodeId") or node_ref),
        "loading": node_status in {"running", "pending", "queued", "generating", "processing"},
        "taskStatus": 2 if succeeded else 1,
        "remoteUrl": "",
        "resourceId": resource_id,
        "promptText": str(gen.get("prompt") or ""),
        "mixedOrder": mixed_order or None,
        "draftPath": draft_path,
        "watermark": False,
        "downloadWarning": download_warning,
        "finalPath": final_path,
        "finalResourceId": final_res_id,
        "finalNodeKey": final_node_key,
    }
