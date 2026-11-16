from __future__ import annotations

import json
import os
import re
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

_AUTH_CACHE_LOCK = threading.Lock()
_AUTH_CACHE: dict[str, Any] = {"fetchedAt": 0.0, "status": None}
_PENDING_LOGIN: dict[str, Any] = {
    "active": False,
    "token": 0,
    "verificationUrl": "",
    "lastError": "",
}


class LibtvAuthRequiredError(RuntimeError):
    """Raised when LibTV CLI is missing credentials or requires web login."""


def _find_libtv_bin() -> str | None:
    candidates = [
        os.environ.get("LIBTV_BIN", "").strip(),
        str(HOME_DIR / ".libtv" / "libtv"),
        str(APP_ROOT / "bin" / "libtv"),
        "/opt/homebrew/bin/libtv",
        "/usr/local/bin/libtv",
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return shutil.which("libtv")


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    prefix = f"{HOME_DIR / '.libtv'}:{HOME_DIR / '.local' / 'bin'}:{APP_ROOT / 'bin'}:/opt/homebrew/bin:/usr/local/bin"
    env["PATH"] = f"{prefix}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def _run_libtv(args: list[str], timeout: int = 45, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    binary = _find_libtv_bin()
    if not binary:
        raise FileNotFoundError("未找到 LibTV CLI（~/.libtv/libtv），请先完成安装。")
    cmd = [binary, *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd or WORKSPACE_ROOT),
        env=_cli_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _extract_json_block(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"未能解析 LibTV JSON 输出: {text[:300]}")
    return json.loads(text[start : end + 1])


def invalidate_auth_cache() -> None:
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE["fetchedAt"] = 0.0
        _AUTH_CACHE["status"] = None


def get_auth_status(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with _AUTH_CACHE_LOCK:
        cached = _AUTH_CACHE.get("status")
        if not force and cached is not None and (now - float(_AUTH_CACHE.get("fetchedAt", 0.0)) < 15.0):
            return dict(cached)
        pending = dict(_PENDING_LOGIN)

    binary = _find_libtv_bin()
    if not binary:
        res = {
            "installed": False,
            "authenticated": False,
            "status": "missing_cli",
            "userUuid": "",
            "accountId": None,
            "loginPending": False,
            "verificationUrl": "",
            "detail": "本机未找到 LibTV CLI（~/.libtv/libtv），请运行安装脚本后再试。",
        }
        with _AUTH_CACHE_LOCK:
            _AUTH_CACHE["fetchedAt"] = now
            _AUTH_CACHE["status"] = dict(res)
        return res

    try:
        proc = _run_libtv(["status"], timeout=15, cwd=HOME_DIR)
        payload = _extract_json_block(proc.stdout)
        auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
        logged_in = bool(auth.get("loggedIn"))
        user_uuid = str(auth.get("userUuid") or "").strip()
        acct_id = auth.get("activeAccountId")
    except Exception as exc:
        return {
            "installed": True,
            "authenticated": False,
            "status": "error",
            "userUuid": "",
            "accountId": None,
            "loginPending": bool(pending.get("active")),
            "verificationUrl": str(pending.get("verificationUrl") or ""),
            "detail": f"检查 LibTV 登录状态失败: {exc}",
        }

    if logged_in:
        with _AUTH_CACHE_LOCK:
            _PENDING_LOGIN["active"] = False
            _PENDING_LOGIN["verificationUrl"] = ""
            _PENDING_LOGIN["lastError"] = ""
        res = {
            "installed": True,
            "authenticated": True,
            "status": "logged_in",
            "userUuid": user_uuid,
            "accountId": acct_id,
            "loginPending": False,
            "verificationUrl": "",
            "detail": f"已连接 LibTV 账号（ID {acct_id or user_uuid[:8] or '已登录'}）",
        }
    else:
        res = {
            "installed": True,
            "authenticated": False,
            "status": "authorization_required" if pending.get("active") else "not_logged_in",
            "userUuid": "",
            "accountId": None,
            "loginPending": bool(pending.get("active")),
            "verificationUrl": str(pending.get("verificationUrl") or ""),
            "detail": (
                "请在打开的 LibTV 网页中完成登录授权…"
                if pending.get("active")
                else (pending.get("lastError") or "LibTV 尚未登录，点击下方按钮在浏览器完成登录后即可加载画布。")
            ),
        }

    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE["fetchedAt"] = now
        _AUTH_CACHE["status"] = dict(res)
    return res


def start_auth_login() -> dict[str, Any]:
    cur = get_auth_status(force=True)
    if cur.get("authenticated"):
        return {"authenticated": True, "status": "logged_in", "verificationUrl": ""}
    binary = _find_libtv_bin()
    if not binary:
        raise FileNotFoundError("未找到 LibTV CLI（~/.libtv/libtv），请先完成安装。")

    with _AUTH_CACHE_LOCK:
        if _PENDING_LOGIN.get("active") and _PENDING_LOGIN.get("verificationUrl"):
            return {
                "authenticated": False,
                "status": "authorization_required",
                "verificationUrl": str(_PENDING_LOGIN["verificationUrl"]),
            }
        _PENDING_LOGIN["token"] = int(_PENDING_LOGIN.get("token", 0)) + 1
        tok = _PENDING_LOGIN["token"]
        _PENDING_LOGIN["active"] = True
        _PENDING_LOGIN["verificationUrl"] = ""
        _PENDING_LOGIN["lastError"] = ""

    proc = subprocess.Popen(
        [binary, "login", "web"],
        cwd=str(HOME_DIR),
        env=_cli_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    found_evt = threading.Event()
    url_holder: list[str] = []
    err_lines: list[str] = []

    def _watch_proc() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                err_lines.append(line)
                if not url_holder:
                    m = re.search(r"https?://[^\s\"'<>]+", line)
                    if m:
                        url_holder.append(m.group(0))
                        with _AUTH_CACHE_LOCK:
                            if _PENDING_LOGIN["token"] == tok:
                                _PENDING_LOGIN["verificationUrl"] = url_holder[0]
                        found_evt.set()
            rc = proc.wait(timeout=240)
            with _AUTH_CACHE_LOCK:
                if _PENDING_LOGIN["token"] == tok:
                    _PENDING_LOGIN["active"] = False
                    if rc == 0:
                        _PENDING_LOGIN["verificationUrl"] = ""
                        _PENDING_LOGIN["lastError"] = ""
                    else:
                        _PENDING_LOGIN["lastError"] = err_lines[-1] if err_lines else "LibTV 网页登录未完成"
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            with _AUTH_CACHE_LOCK:
                if _PENDING_LOGIN["token"] == tok:
                    _PENDING_LOGIN["active"] = False
                    _PENDING_LOGIN["lastError"] = str(exc)
        finally:
            found_evt.set()
            invalidate_auth_cache()

    threading.Thread(target=_watch_proc, name="libtv-web-login", daemon=True).start()
    found_evt.wait(timeout=8.0)

    if url_holder:
        return {"authenticated": False, "status": "authorization_required", "verificationUrl": url_holder[0]}
    st_after = get_auth_status(force=True)
    if st_after.get("authenticated"):
        return {"authenticated": True, "status": "logged_in", "verificationUrl": ""}
    raise RuntimeError(err_lines[-1] if err_lines else "未能获取 LibTV 网页登录链接")


def list_workspaces() -> dict[str, str]:
    """Return mapping of workspace/folder id -> name across the account."""
    ws_map: dict[str, str] = {}
    home_dir = Path.home()
    for page in range(1, 6):
        proc = _run_libtv(["workspace", "list", "-p", str(page), "-s", "100"], timeout=20, cwd=home_dir)
        if proc.returncode != 0:
            break
        payload = _extract_json_block(proc.stdout)
        folders = payload.get("folders") or []
        for f in folders:
            fid = str(f.get("id") or "").strip()
            fname = str(f.get("name") or "").strip()
            if fid and fname:
                ws_map[fid] = fname
        total = int(payload.get("total") or 0)
        if len(folders) < 100 or (total and len(ws_map) >= total):
            break
    return ws_map


def list_projects() -> list[dict[str, Any]]:
    auth_st = get_auth_status(force=False)
    if not auth_st.get("authenticated"):
        raise LibtvAuthRequiredError(str(auth_st.get("detail") or "LibTV 尚未登录"))

    home_dir = Path.home()
    try:
        ws_map = list_workspaces()
    except Exception:
        ws_map = {}

    raw_items: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()
    for page in range(1, 6):
        # Run from home_dir (no .libtv/project.json) so list includes canvases from all workspaces & root.
        proc = _run_libtv(["project", "list", "-p", str(page), "-s", "100"], timeout=20, cwd=home_dir)
        if proc.returncode != 0:
            err_txt = proc.stderr.strip() or proc.stdout.strip() or "libtv project list 失败"
            if any(k in err_txt.lower() for k in ("login", "token", "401", "unauthorized", "未登录", "凭据")):
                invalidate_auth_cache()
                raise LibtvAuthRequiredError(err_txt)
            if page == 1:
                raise RuntimeError(err_txt)
            break
        payload = _extract_json_block(proc.stdout)
        items = payload.get("projectMetaList", []) or []
        for p in items:
            uid = p.get("uuid")
            if uid and uid not in seen_uuids:
                seen_uuids.add(uid)
                raw_items.append(p)
        total = int(payload.get("total") or 0)
        if len(items) < 100 or (total and len(raw_items) >= total):
            break

    projects: list[dict[str, Any]] = []
    for p in raw_items:
        uid = p.get("uuid", "")
        ws_id = p.get("projectSpaceId") or p.get("folderId")
        ws_name = ws_map.get(str(ws_id), "").strip() if ws_id else ""
        raw_name = str(p.get("name") or "").strip()
        if ws_name and raw_name and ws_name != raw_name:
            display_name = f"{ws_name} · {raw_name}"
        else:
            display_name = raw_name or ws_name or uid
        projects.append(
            {
                "uuid": uid,
                "name": display_name,
                "rawName": raw_name or uid,
                "workspaceName": ws_name,
                "id": p.get("id"),
                "workspaceId": ws_id,
                "updatedAtMs": p.get("updatedAtMs"),
                "url": (
                    f"https://www.liblib.tv/canvas?projectId={uid}"
                    + (f"&spaceId={ws_id}" if ws_id else "")
                ),
            }
        )
    return projects


def create_project(
    name: str,
    workspace_id: str | int | None = "0",
    create_workspace: bool = False,
    description: str = "",
) -> dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("画布名称不能为空")

    home_dir = Path.home()
    target_ws = str(workspace_id).strip() if workspace_id is not None and str(workspace_id).strip() != "" else "0"
    ws_name = ""

    if create_workspace:
        ws_args = ["workspace", "create", clean_name]
        if description.strip():
            ws_args.extend(["-d", description.strip()])
        ws_proc = _run_libtv(ws_args, timeout=25, cwd=home_dir)
        if ws_proc.returncode != 0:
            raise RuntimeError(ws_proc.stderr.strip() or ws_proc.stdout.strip() or "libtv workspace create 失败")
        ws_payload = _extract_json_block(ws_proc.stdout)
        created_ws = ws_payload.get("workspaceId") or ws_payload.get("id") or (ws_payload.get("folder") or {}).get("id")
        if not created_ws:
            raise RuntimeError(f"已创建项目工作区但未返回 workspaceId: {ws_proc.stdout[:200]}")
        target_ws = str(created_ws)
        ws_name = clean_name

    args = ["project", "create", clean_name, "-w", target_ws]
    if description.strip():
        args.extend(["-d", description.strip()])
    proc = _run_libtv(args, timeout=25, cwd=home_dir)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "libtv project create 失败")

    payload = _extract_json_block(proc.stdout)
    meta = payload.get("projectMeta") if isinstance(payload.get("projectMeta"), dict) else payload
    proj_uuid = str(meta.get("uuid") or payload.get("uuid") or "").strip()
    if not proj_uuid:
        raise RuntimeError(f"已创建画布但未返回 uuid: {proc.stdout[:200]}")

    raw_name = str(meta.get("name") or clean_name).strip()
    eff_ws_id = meta.get("projectSpaceId") or meta.get("folderId") or (int(target_ws) if target_ws.isdigit() and target_ws != "0" else None)
    if not ws_name and eff_ws_id:
        try:
            ws_name = list_workspaces().get(str(eff_ws_id), "")
        except Exception:
            ws_name = ""

    display_name = f"{ws_name} · {raw_name}" if ws_name and raw_name and ws_name != raw_name else (raw_name or ws_name or proj_uuid)
    return {
        "uuid": proj_uuid,
        "name": display_name,
        "rawName": raw_name,
        "workspaceName": ws_name,
        "id": meta.get("id"),
        "workspaceId": eff_ws_id,
        "updatedAtMs": meta.get("updatedAtMs"),
        "url": (
            f"https://www.liblib.tv/canvas?projectId={proj_uuid}"
            + (f"&spaceId={eff_ws_id}" if eff_ws_id else "")
        ),
    }


def inspect_node(node_ref: str, project_uuid: str | None = None, group_ref: str | None = None) -> dict[str, Any]:
    args = ["node", node_ref]
    if project_uuid:
        args.extend(["-p", project_uuid])
    if group_ref:
        args.extend(["-g", group_ref])
    proc = _run_libtv(args, timeout=25)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"读取节点失败: {node_ref}")
    return _extract_json_block(proc.stdout)


def download_node_video(
    node_ref: str,
    dest_file: Path,
    project_uuid: str | None = None,
    group_ref: str | None = None,
) -> tuple[Path, bool]:
    """Download video asset from a LibTV node cleanly into `dest_file` (tries --watermark false then true)."""
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for watermark_flag in ("false", "true"):
        with tempfile.TemporaryDirectory(prefix="libtv_dl_", dir=str(dest_file.parent)) as tmp_dir:
            tmp_path = Path(tmp_dir)
            args = ["download", "-n", node_ref, "-o", str(tmp_path), "--watermark", watermark_flag]
            if project_uuid:
                args.extend(["-p", project_uuid])
            if group_ref:
                args.extend(["-g", group_ref])
            proc = _run_libtv(args, timeout=180)
            if proc.returncode != 0:
                last_err = (proc.stderr or proc.stdout or "").strip()
                continue
            videos = sorted(
                [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}],
                key=lambda p: p.stat().st_size,
                reverse=True,
            )
            if not videos:
                last_err = f"libtv download 执行成功但未找到视频文件 ({proc.stdout.strip()})"
                continue
            tmp_target = dest_file.with_name(f".{dest_file.name}.tmp")
            shutil.copy2(videos[0], tmp_target)
            os.replace(tmp_target, dest_file)
            return dest_file, (watermark_flag == "true")
    raise RuntimeError(last_err or f"下载节点 {node_ref} 失败")


def sync_shot_node(shot: dict[str, Any], shot_dir: Path) -> dict[str, Any]:
    """Inspect LibTV sample node and pull completed 480P draft video if available."""
    shot_id = shot["id"]
    project_uuid = shot.get("projectUuid") or None
    group_ref = shot.get("libtvGroupKey") or None
    node_ref = shot.get("libtvSampleNodeKey") or f"{shot_id}-样片"

    node_data = inspect_node(node_ref, project_uuid=project_uuid, group_ref=group_ref)
    data = node_data.get("data", {})
    params = data.get("params", {})
    task_info = data.get("taskInfo") or {}
    urls = data.get("url") or []
    remote_url = urls[0] if isinstance(urls, list) and urls else ""

    mixed_order = []
    for idx, item in enumerate(params.get("mixedList", []), start=1):
        mixed_order.append(
            {
                "index": idx,
                "mediaType": item.get("mediaType", ""),
                "nodeKey": item.get("nodeId", ""),
                "label": item.get("label", ""),
            }
        )

    draft_path: Path | None = None
    watermark_used: bool | None = None
    existing_draft = shot.get("draftVideo") or {}
    if remote_url:
        target = shot_dir / "draft_480p.mp4"
        need_download = (
            not target.is_file()
            or existing_draft.get("remoteUrl") != remote_url
        )
        if need_download:
            draft_path, watermark_used = download_node_video(
                node_data.get("nodeKey") or node_ref,
                target,
                project_uuid=project_uuid,
                group_ref=group_ref if not node_data.get("nodeKey") else None,
            )
        else:
            draft_path = target
            watermark_used = existing_draft.get("watermark")

    final_path: Path | None = None
    remote_final_url = ""
    final_watermark: bool | None = None
    final_node_key = str(shot.get("libtvFinalNodeKey") or "").strip()
    if final_node_key:
        try:
            fnode_data = inspect_node(final_node_key, project_uuid=project_uuid, group_ref=group_ref)
            fdata = fnode_data.get("data", {})
            furls = fdata.get("url") or []
            remote_final_url = furls[0] if isinstance(furls, list) and furls else ""
            existing_final = shot.get("finalVideo") or {}
            ftarget = shot_dir / "final_1080p.mp4"
            if remote_final_url:
                if not ftarget.is_file() or existing_final.get("remoteUrl") != remote_final_url:
                    final_path, final_watermark = download_node_video(
                        fnode_data.get("nodeKey") or final_node_key,
                        ftarget,
                        project_uuid=project_uuid,
                        group_ref=group_ref if not fnode_data.get("nodeKey") else None,
                    )
                else:
                    final_path = ftarget
                    final_watermark = existing_final.get("watermark")
            final_node_key = fnode_data.get("nodeKey") or final_node_key
        except Exception:
            pass

    return {
        "nodeKey": node_data.get("nodeKey") or node_ref,
        "loading": bool(task_info.get("loading")),
        "taskStatus": task_info.get("status"),
        "remoteUrl": remote_url,
        "promptText": params.get("prompt") or "",
        "mixedOrder": mixed_order or None,
        "draftPath": draft_path,
        "watermark": watermark_used,
        "finalPath": final_path,
        "finalRemoteUrl": remote_final_url,
        "finalWatermark": final_watermark,
        "finalNodeKey": final_node_key,
    }
