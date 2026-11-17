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

from store import WORKSPACE_ROOT, _parse_timestamp_ms, replace_shot_video_with_archive

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
        replace_shot_video_with_archive(tmp_target, dest_file)
        return dest_file


_TERMINAL_SUCCEEDED = {"succeeded", "success", "completed"}


def _resource_created_ms(res: dict[str, Any] | None) -> int:
    if not isinstance(res, dict):
        return 0
    try:
        return int(res.get("createdAt") or 0)
    except (TypeError, ValueError):
        return 0


def _pick_latest_succeeded_resource(resources: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Return newest succeeded resource by `createdAt` descending (list order tie-breaker)."""
    succeeded = [
        r
        for r in (resources or [])
        if isinstance(r, dict) and str(r.get("status") or "").lower() in _TERMINAL_SUCCEEDED
    ]
    if not succeeded:
        return None
    return max(enumerate(succeeded), key=lambda pair: (_resource_created_ms(pair[1]), -pair[0]))[1]


def find_project_video_nodes(project_uuid: str, name_filter: str = "") -> list[dict[str, Any]]:
    """List video nodes on a Dreamina canvas project (handles 50-item pagination)."""
    proj = str(project_uuid or "").strip()
    if not proj:
        return []
    items: list[dict[str, Any]] = []
    offset = 0
    for _ in range(12):
        args = [
            "node",
            "find",
            "--project-id",
            proj,
            "--type",
            "video",
            "--limit",
            "50",
            "--offset",
            str(offset),
        ]
        if name_filter.strip():
            args.extend(["--name", name_filter.strip()])
        proc = _run_dreamina(args, timeout=25)
        try:
            payload = _extract_json(proc)
        except Exception:
            payload = None
        if proc.returncode != 0 or not (isinstance(payload, dict) and payload.get("ok")):
            if _is_auth_error(proc, payload):
                invalidate_auth_cache()
                raise JimengAuthRequiredError("即梦官网尚未登录授权")
            raise RuntimeError(_extract_error_msg(payload, proc.stderr.strip() or f"扫描即梦画布视频节点失败: {proj}"))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        page = [it for it in (data.get("items") or []) if isinstance(it, dict)]
        items.extend(page)
        if not data.get("hasMore") or not page:
            break
        offset += len(page)
    return items


def _classify_shot_canvas_nodes(
    shot_id: str,
    project_videos: list[dict[str, Any]] | None,
) -> tuple[list[str], list[str]]:
    """Return `(sample_node_ids, final_node_ids)` matching `shot_id` on Dreamina canvas."""
    sid_upper = str(shot_id or "").strip().upper()
    if not sid_upper or not project_videos:
        return [], []
    samples: list[str] = []
    finals: list[str] = []
    seen_s: set[str] = set()
    seen_f: set[str] = set()
    for it in project_videos:
        nid = str(it.get("nodeId") or it.get("id") or "").strip()
        if not nid.startswith("node_"):
            continue
        title = str(it.get("title") or "").strip()
        t_up = title.upper()
        if not (t_up == sid_upper or t_up.startswith(f"{sid_upper}-") or t_up.startswith(f"{sid_upper}_")):
            continue
        if any(skip in title for skip in ("原片", "深度", "景深")):
            continue
        if "1080" in title or "正片" in title:
            if nid not in seen_f:
                seen_f.add(nid)
                finals.append(nid)
        elif "样片" in title:
            if nid not in seen_s:
                seen_s.add(nid)
                samples.append(nid)
    return samples, finals


def _inspect_dreamina_nodes_batch(node_ids: list[str], project_uuid: str) -> dict[str, dict[str, Any]]:
    clean_ids: list[str] = []
    seen: set[str] = set()
    for nid in node_ids:
        s = str(nid or "").strip()
        if s.startswith("node_") and s not in seen:
            seen.add(s)
            clean_ids.append(s)
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(clean_ids), 20):
        chunk = clean_ids[i : i + 20]
        proc = _run_dreamina(
            ["node", "show", "--node-id", ",".join(chunk), "--project-id", project_uuid],
            timeout=30,
        )
        try:
            payload = _extract_json(proc)
        except Exception:
            payload = None
        if proc.returncode != 0 or not (isinstance(payload, dict) and payload.get("ok")):
            if _is_auth_error(proc, payload):
                invalidate_auth_cache()
                raise JimengAuthRequiredError("即梦官网尚未登录授权")
            raise RuntimeError(
                _extract_error_msg(payload, proc.stderr.strip() or f"读取即梦节点失败: {','.join(chunk)}")
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for row in data.get("nodes") or []:
            if not isinstance(row, dict):
                continue
            node = row.get("node")
            if isinstance(node, dict) and node.get("nodeId"):
                out[str(node["nodeId"])] = node
    return out


def _inspect_dreamina_node_raw(node_id: str, project_uuid: str) -> dict[str, Any]:
    found = _inspect_dreamina_nodes_batch([node_id], project_uuid)
    node = found.get(str(node_id).strip())
    if not node:
        raise RuntimeError(f"即梦画布中未找到节点 {node_id}")
    return node


def _is_final_1080_node(node: dict[str, Any] | None) -> bool:
    if not isinstance(node, dict):
        return False
    title = str(node.get("title") or "")
    if "1080" in title or "正片" in title:
        return True
    for r in node.get("resources") or []:
        if not isinstance(r, dict):
            continue
        vid = r.get("video") if isinstance(r.get("video"), dict) else {}
        res_str = str(vid.get("resolution") or "").lower()
        w = int(vid.get("width") or 0)
        if "1080" in res_str or w >= 1920:
            return True
    return False


def _node_latest_resource_ms(node: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(node, dict):
        return (0, 0)
    best = _pick_latest_succeeded_resource(node.get("resources"))
    if best is not None:
        return (1, _resource_created_ms(best))
    return (0, 0)


def prefetch_canvas_sync_context(
    shots: list[dict[str, Any]],
    fallback_project_uuid: str = "",
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, dict[str, Any]]]]:
    """Batch-fetch Dreamina project video lists & node details once per project for rapid sync-all."""
    proj_ids: list[str] = []
    seen_p: set[str] = set()
    for p in [fallback_project_uuid] + [str(s.get("projectUuid") or "").strip() for s in shots]:
        if p and p not in seen_p:
            seen_p.add(p)
            proj_ids.append(p)

    videos_by_proj: dict[str, list[dict[str, Any]]] = {}
    nodes_by_proj: dict[str, dict[str, dict[str, Any]]] = {}
    for p_uuid in proj_ids:
        vlist = find_project_video_nodes(p_uuid)
        videos_by_proj[p_uuid] = vlist
        cand_ids: list[str] = []
        for s in shots:
            s_proj = str(s.get("projectUuid") or "").strip()
            if s_proj != p_uuid and fallback_project_uuid != p_uuid:
                continue
            jm = s.get("jimengMeta") if isinstance(s.get("jimengMeta"), dict) else {}
            for k in (jm.get("sampleNodeId"), jm.get("nodeId"), s.get("libtvSampleNodeKey"), jm.get("finalNodeId"), jm.get("upscaleNodeId"), s.get("libtvFinalNodeKey")):
                ks = str(k or "").strip()
                if ks.startswith("node_"):
                    cand_ids.append(ks)
            disc_s, disc_f = _classify_shot_canvas_nodes(str(s.get("id") or ""), vlist)
            cand_ids.extend(disc_s)
            cand_ids.extend(disc_f)
        node_map = _inspect_dreamina_nodes_batch(cand_ids, p_uuid) if cand_ids else {}
        # Second tiny batch for any downstream 1080P nodes not yet inspected
        extra_downstream: list[str] = []
        for nd in node_map.values():
            for dnid in nd.get("downstreamNodeIds") or []:
                ds = str(dnid or "").strip()
                if ds.startswith("node_") and ds not in node_map:
                    extra_downstream.append(ds)
        if extra_downstream:
            node_map.update(_inspect_dreamina_nodes_batch(extra_downstream, p_uuid))
        nodes_by_proj[p_uuid] = node_map
    return videos_by_proj, nodes_by_proj


def sync_shot_node(
    shot: dict[str, Any],
    shot_dir: Path,
    *,
    fallback_project_uuid: str = "",
    prefetched_videos_by_proj: dict[str, list[dict[str, Any]]] | None = None,
    prefetched_nodes_by_proj: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Inspect Dreamina sample & 1080P final nodes (including newly promoted/duplicated canvas nodes) and pull latest outputs."""
    sid = str(shot.get("id") or "").strip()
    jm = shot.get("jimengMeta") if isinstance(shot.get("jimengMeta"), dict) else {}
    orig_proj = str(shot.get("projectUuid") or jm.get("projectId") or "").strip()
    fb_proj = str(fallback_project_uuid or "").strip()
    if not orig_proj and not fb_proj:
        raise ValueError("当前镜头尚未绑定即梦画布，无法拉取节点状态")

    # Prioritize genuine Dreamina node_* IDs over stale LibTV v-* keys
    raw_sample_refs = [jm.get("sampleNodeId"), jm.get("nodeId"), shot.get("libtvSampleNodeKey")]
    bound_sample_id = next((str(x).strip() for x in raw_sample_refs if str(x or "").strip().startswith("node_")), "")
    raw_final_refs = [jm.get("finalNodeId"), jm.get("upscaleNodeId"), shot.get("libtvFinalNodeKey")]
    bound_final_id = next((str(x).strip() for x in raw_final_refs if str(x or "").strip().startswith("node_")), "")

    proj_candidates: list[str] = []
    for p in (orig_proj, fb_proj):
        if p and p not in proj_candidates:
            proj_candidates.append(p)

    resolved_proj = orig_proj or fb_proj
    sample_node: dict[str, Any] | None = None
    node_cache: dict[str, dict[str, Any]] = {}
    disc_samples: list[str] = []
    disc_finals: list[str] = []

    for p_uuid in proj_candidates:
        if prefetched_videos_by_proj is not None and p_uuid in prefetched_videos_by_proj:
            vlist = prefetched_videos_by_proj[p_uuid]
        else:
            vlist = find_project_video_nodes(p_uuid, name_filter=f"{sid}-") if sid else []
        p_cache = (prefetched_nodes_by_proj or {}).get(p_uuid)
        local_cache: dict[str, dict[str, Any]] = dict(p_cache) if isinstance(p_cache, dict) else {}

        ds, df = _classify_shot_canvas_nodes(sid, vlist)
        to_fetch: list[str] = []
        for cid in [bound_sample_id, bound_final_id, *ds, *df]:
            if cid and cid not in local_cache and cid not in to_fetch:
                to_fetch.append(cid)
        if to_fetch:
            local_cache.update(_inspect_dreamina_nodes_batch(to_fetch, p_uuid))

        sample_cands = []
        for cid in [bound_sample_id, *ds]:
            if cid and cid in local_cache and cid not in sample_cands:
                sample_cands.append(cid)
        if sample_cands or df or (bound_final_id and bound_final_id in local_cache):
            resolved_proj = p_uuid
            node_cache = local_cache
            disc_samples = ds
            disc_finals = df
            if sample_cands:
                # Choose sample node with the newest succeeded resource (prefer bound_sample_id on ties)
                best_sid = max(
                    sample_cands,
                    key=lambda cid: (_node_latest_resource_ms(node_cache.get(cid)), 1 if cid == bound_sample_id else 0),
                )
                sample_node = node_cache[best_sid]
            break

    if sample_node is None and not disc_finals and not (bound_final_id and bound_final_id in node_cache):
        raise ValueError(f"在即梦画布上未找到 {sid} 对应的视频节点")

    node = sample_node or {}
    node_ref = str(node.get("nodeId") or bound_sample_id or "").strip()
    gen = node.get("generation") if isinstance(node.get("generation"), dict) else {}
    resources = [r for r in (node.get("resources") or []) if isinstance(r, dict)]
    node_status = str(node.get("status") or "").lower()

    chosen_res = _pick_latest_succeeded_resource(resources)
    resource_id = str((chosen_res or {}).get("resourceId") or "").strip()
    draft_created_ms = _resource_created_ms(chosen_res)

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

    pv = (
        shot.get("passedVersion")
        if str(shot.get("status") or "") in {"final_ready", "final_done"}
        and not shot.get("finalModifiedReopened")
        and isinstance(shot.get("passedVersion"), dict)
        else None
    )
    pv_role = str((pv or {}).get("role") or "").strip().lower()
    pv_marked_ms = _parse_timestamp_ms((pv or {}).get("markedAt"))

    draft_path: Path | None = None
    download_warning = ""
    existing_dl_res = str(jm.get("downloadedResourceId") or (shot.get("draftVideo") or {}).get("resourceId") or "").strip()
    target = shot_dir / "draft_480p.mp4"
    lock_draft_target = bool(
        pv
        and pv_role == "480p"
        and target.is_file()
        and (draft_created_ms == 0 or pv_marked_ms <= 0 or draft_created_ms <= pv_marked_ms)
    )
    if lock_draft_target:
        draft_path = None
    elif resource_id:
        if not target.is_file() or existing_dl_res != resource_id:
            try:
                draft_path = download_resource(resource_id, target, project_uuid=resolved_proj)
            except JimengAuthRequiredError:
                raise
            except Exception as dl_exc:
                download_warning = str(dl_exc)
                if target.is_file():
                    draft_path = target
        else:
            draft_path = target

    # Discover & check 1080P final nodes (bound + downstream of sample + title-matched `{sid}-*正片/1080*`)
    downstream_ids = [str(x).strip() for x in (node.get("downstreamNodeIds") or []) if str(x or "").strip().startswith("node_")]
    missing_down = [x for x in downstream_ids if x not in node_cache]
    if missing_down:
        try:
            node_cache.update(_inspect_dreamina_nodes_batch(missing_down, resolved_proj))
        except JimengAuthRequiredError:
            raise
        except Exception:
            pass

    final_cands: list[str] = []
    for cid in [bound_final_id, *disc_finals, *[d for d in downstream_ids if _is_final_1080_node(node_cache.get(d))]]:
        if cid and cid in node_cache and cid not in final_cands:
            if cid in {bound_final_id, *disc_finals} or _is_final_1080_node(node_cache.get(cid)):
                final_cands.append(cid)

    final_path: Path | None = None
    final_res_id = ""
    final_created_ms = 0
    final_node_key = bound_final_id
    if final_cands:
        best_fid = max(
            final_cands,
            key=lambda cid: (_node_latest_resource_ms(node_cache.get(cid)), 1 if cid == bound_final_id else 0),
        )
        fnode = node_cache.get(best_fid) or {}
        fresources = [r for r in (fnode.get("resources") or []) if isinstance(r, dict)]
        chosen_fres = _pick_latest_succeeded_resource(fresources)
        if chosen_fres:
            final_res_id = str(chosen_fres.get("resourceId") or "").strip()
            final_created_ms = _resource_created_ms(chosen_fres)
        final_node_key = str(fnode.get("nodeId") or best_fid)
        existing_final_res = str(
            jm.get("downloadedFinalResourceId")
            or (shot.get("finalVideo") or {}).get("resourceId")
            or ""
        ).strip()
        ftarget = shot_dir / "final_1080p.mp4"
        lock_final_target = bool(
            pv
            and pv_role in {"480p", "1080p"}
            and (final_created_ms == 0 or pv_marked_ms <= 0 or final_created_ms <= pv_marked_ms)
        )
        if lock_final_target:
            final_path = None
        elif final_res_id:
            if not ftarget.is_file() or existing_final_res != final_res_id:
                try:
                    final_path = download_resource(final_res_id, ftarget, project_uuid=resolved_proj)
                except JimengAuthRequiredError:
                    raise
                except Exception as fin_exc:
                    if not download_warning:
                        download_warning = str(fin_exc)
                    if ftarget.is_file():
                        final_path = ftarget
            else:
                final_path = ftarget

    return {
        "projectUuid": resolved_proj,
        "nodeKey": node_ref,
        "loading": node_status in {"running", "pending", "queued", "generating", "processing"},
        "taskStatus": 2 if (chosen_res is not None or final_res_id) else 1,
        "remoteUrl": "",
        "resourceId": resource_id,
        "draftCreatedAtMs": draft_created_ms,
        "promptText": str(gen.get("prompt") or ""),
        "mixedOrder": mixed_order or None,
        "draftPath": draft_path,
        "watermark": False,
        "downloadWarning": download_warning,
        "finalPath": final_path,
        "finalWatermark": False,
        "finalResourceId": final_res_id,
        "finalCreatedAtMs": final_created_ms,
        "finalNodeKey": final_node_key,
    }
