#!/usr/bin/env python3
"""GitHub hot-update engine for Wash Queue Console (zero git binary required)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

APP_ROOT = Path(__file__).resolve().parent
VERSION_FILE = APP_ROOT / "version.json"
USER_CONF_DIR = Path.home() / ".wash_console"
UPDATER_CONF_PATH = USER_CONF_DIR / "updater.json"
BACKUP_DIR = USER_CONF_DIR / "backup_prev"

DEFAULT_REPO = "hqy-maker/wash-queue-console"
DEFAULT_BRANCH = "main"

APP_MANAGED_FILES = [
    "server.py",
    "store.py",
    "updater.py",
    "shot_fastpath.py",
    "libtv_bridge.py",
    "jimeng_bridge.py",
    "agent_bridge.py",
    "launch.sh",
    "requirements.txt",
    "version.json",
]
APP_MANAGED_DIRS = ["static", "scripts"]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_version_tuple(version: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", str(version or ""))]
    return tuple(nums) if nums else (0,)


def is_remote_newer(
    local_version: str,
    remote_version: str,
    *,
    local_published: str = "",
    remote_published: str = "",
) -> bool:
    rv = str(remote_version or "").strip()
    lv = str(local_version or "").strip()
    if not rv:
        return False
    lt = parse_version_tuple(lv)
    rt = parse_version_tuple(rv)
    if rt != lt:
        return rt > lt
    if rv != lv and remote_published and local_published:
        return remote_published > local_published
    return False


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    root_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = (dest_dir / member.filename).resolve()
            if member_path != root_resolved and root_resolved not in member_path.parents:
                raise ValueError(f"压缩包内存在非法路径: {member.filename}")
        zf.extractall(dest_dir)


def _locate_bundle_root(extract_dir: Path) -> Path:
    if (extract_dir / "version.json").is_file() and (extract_dir / "app" / "server.py").is_file():
        return extract_dir
    for child in extract_dir.iterdir():
        if child.is_dir() and (child / "version.json").is_file() and (child / "app" / "server.py").is_file():
            return child
    raise RuntimeError("更新包缺少 version.json 或 app/server.py，已取消更新以保护现有程序")


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class HotUpdateManager:
    """Checks, verifies, backs up, applies, and rolls back GitHub hot updates."""

    def __init__(self, *, app_root: Path | None = None, backup_dir: Path | None = None) -> None:
        self.app_root = (app_root or APP_ROOT).resolve()
        self.version_file = self.app_root / "version.json"
        self.backup_dir = (backup_dir or BACKUP_DIR).resolve()
        self._lock = threading.Lock()
        self._applying = False
        self._cached_check: dict[str, Any] | None = None

    def get_config(self) -> dict[str, str]:
        local_meta = _read_json_file(self.version_file)
        user_cfg = _read_json_file(UPDATER_CONF_PATH)
        repo = str(user_cfg.get("repo") or local_meta.get("repo") or DEFAULT_REPO).strip()
        branch = str(user_cfg.get("branch") or local_meta.get("branch") or DEFAULT_BRANCH).strip()
        token = str(os.environ.get("WASH_GITHUB_TOKEN") or user_cfg.get("token") or "").strip()
        base_url = str(os.environ.get("WASH_UPDATER_BASE_URL") or user_cfg.get("baseUrl") or "").strip().rstrip("/")
        return {
            "repo": repo,
            "branch": branch,
            "token": token,
            "baseUrl": base_url,
        }

    def get_local_manifest(self) -> dict[str, Any]:
        meta = _read_json_file(self.version_file)
        if not meta:
            meta = {"version": "0.0.0", "repo": DEFAULT_REPO, "branch": DEFAULT_BRANCH}
        return meta

    def current_version(self) -> str:
        return str(self.get_local_manifest().get("version") or "0.0.0")

    def _headers(self, token: str, *, accept: str = "application/json") -> dict[str, str]:
        hdrs = {
            "User-Agent": "WashQueueConsole-Updater/1.0",
            "Accept": accept,
        }
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        return hdrs

    def _fetch_remote_manifest(self, cfg: dict[str, str]) -> dict[str, Any]:
        repo = cfg["repo"]
        branch = cfg["branch"]
        token = cfg["token"]
        base_url = cfg["baseUrl"]

        urls: list[tuple[str, dict[str, str]]] = []
        if base_url:
            urls.append((f"{base_url}/version.json?ts={int(time.time())}", self._headers(token)))
        else:
            urls.append(
                (
                    f"https://api.github.com/repos/{repo}/contents/version.json?ref={branch}&ts={int(time.time())}",
                    self._headers(token, accept="application/vnd.github.raw+json"),
                )
            )
            urls.append(
                (
                    f"https://raw.githubusercontent.com/{repo}/{branch}/version.json?ts={int(time.time())}",
                    self._headers(token),
                )
            )

        last_err = ""
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            for url, hdrs in urls:
                try:
                    resp = client.get(url, headers=hdrs)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("version"):
                            return data
                        # Handle API base64 response if raw accept wasn't honored
                        if isinstance(data, dict) and data.get("content"):
                            raw_bytes = base64.b64decode(str(data["content"]))
                            parsed = json.loads(raw_bytes.decode("utf-8"))
                            if isinstance(parsed, dict) and parsed.get("version"):
                                return parsed
                    last_err = f"HTTP {resp.status_code}"
                except Exception as exc:
                    last_err = str(exc)
        raise RuntimeError(f"无法读取 GitHub 版本清单（{repo}@{branch}）：{last_err or '网络不可用'}")

    def _backup_meta(self) -> dict[str, Any]:
        return _read_json_file(self.backup_dir / "backup_meta.json")

    def get_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        cfg = self.get_config()
        local = self.get_local_manifest()
        local_ver = str(local.get("version") or "0.0.0")
        local_pub = str(local.get("publishedAt") or "")
        backup = self._backup_meta()

        remote: dict[str, Any] | None = None
        err_msg = ""
        if force_refresh or self._cached_check is None:
            try:
                remote = self._fetch_remote_manifest(cfg)
                self._cached_check = {
                    "remote": remote,
                    "checkedAt": _now_iso(),
                    "error": "",
                }
            except Exception as exc:
                err_msg = str(exc)
                prev_remote = (self._cached_check or {}).get("remote")
                self._cached_check = {
                    "remote": prev_remote,
                    "checkedAt": _now_iso(),
                    "error": err_msg,
                }
                remote = prev_remote
        else:
            remote = self._cached_check.get("remote")
            err_msg = str(self._cached_check.get("error") or "")

        remote_ver = str((remote or {}).get("version") or "")
        remote_pub = str((remote or {}).get("publishedAt") or "")
        remote_changelog = str((remote or {}).get("changelog") or "")
        has_update = bool(
            remote_ver
            and is_remote_newer(
                local_ver,
                remote_ver,
                local_published=local_pub,
                remote_published=remote_pub,
            )
        )
        return {
            "ok": not bool(err_msg) or bool(remote),
            "repo": cfg["repo"],
            "branch": cfg["branch"],
            "repoUrl": f"https://github.com/{cfg['repo']}",
            "currentVersion": local_ver,
            "currentPublishedAt": local_pub,
            "currentChangelog": str(local.get("changelog") or ""),
            "latestVersion": remote_ver or local_ver,
            "latestPublishedAt": remote_pub,
            "changelog": remote_changelog,
            "hasUpdate": has_update,
            "checkedAt": str((self._cached_check or {}).get("checkedAt") or ""),
            "applying": self._applying,
            "canRollback": bool(backup.get("version") and (self.backup_dir / "app").is_dir()),
            "backupVersion": str(backup.get("version") or ""),
            "error": err_msg,
        }

    def _download_archive(self, cfg: dict[str, str], remote: dict[str, Any], dest_zip: Path) -> None:
        repo = cfg["repo"]
        branch = cfg["branch"]
        token = cfg["token"]
        base_url = cfg["baseUrl"]

        candidates: list[str] = []
        if remote.get("archiveUrl"):
            candidates.append(str(remote["archiveUrl"]))
        if base_url:
            candidates.append(f"{base_url}/archive.zip")
        else:
            candidates.append(f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}")
            candidates.append(f"https://api.github.com/repos/{repo}/zipball/{branch}")

        last_err = ""
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            for url in candidates:
                try:
                    with client.stream("GET", url, headers=self._headers(token)) as resp:
                        if resp.status_code != 200:
                            last_err = f"HTTP {resp.status_code} ({url})"
                            continue
                        with dest_zip.open("wb") as fp:
                            for chunk in resp.iter_bytes(65536):
                                fp.write(chunk)
                        if dest_zip.stat().st_size > 1024:
                            return
                        last_err = "下载到的更新包过小"
                except Exception as exc:
                    last_err = str(exc)
        raise RuntimeError(f"下载 GitHub 更新包失败：{last_err or '未知错误'}")

    def _verify_staged_app(self, staged_app: Path) -> None:
        required = [
            "server.py",
            "store.py",
            "updater.py",
            "static/app.js",
            "static/index.html",
            "static/styles.css",
            "launch.sh",
        ]
        missing = [rel for rel in required if not (staged_app / rel).is_file()]
        if missing:
            raise RuntimeError(f"更新包结构不完整，缺少文件: {', '.join(missing)}")
        py_files = [str(p) for p in staged_app.rglob("*.py")]
        if py_files:
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", *py_files],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"更新包 Python 语法预检失败: {proc.stderr.strip() or proc.stdout.strip()}")

    def _skill_target_roots(self, workspace_root: Path | None) -> list[Path]:
        roots = [Path.home() / ".gemini" / "config" / "skills"]
        if (self.app_root.parent / "skills").is_dir() or self.app_root.parent.name == "Resources":
            roots.append(self.app_root.parent / "skills")
        if workspace_root is not None:
            roots.append(workspace_root.resolve() / ".agents" / "skills")
        dedup: list[Path] = []
        for r in roots:
            rr = r.resolve()
            if rr not in dedup:
                dedup.append(rr)
        return dedup

    def _create_backup(self, current_ver: str, skill_names: list[str], workspace_root: Path | None) -> None:
        tmp_backup = self.backup_dir.parent / f"backup_tmp_{os.getpid()}"
        if tmp_backup.exists():
            shutil.rmtree(tmp_backup, ignore_errors=True)
        tmp_app = tmp_backup / "app"
        tmp_skills = tmp_backup / "skills"
        tmp_app.mkdir(parents=True, exist_ok=True)
        tmp_skills.mkdir(parents=True, exist_ok=True)

        for fname in APP_MANAGED_FILES:
            src = self.app_root / fname
            if src.is_file():
                shutil.copy2(src, tmp_app / fname)
        for dname in APP_MANAGED_DIRS:
            src_d = self.app_root / dname
            if src_d.is_dir():
                shutil.copytree(
                    src_d,
                    tmp_app / dname,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
                    dirs_exist_ok=True,
                )

        # Preserve prior skills that will be overwritten
        primary_skill_root = Path.home() / ".gemini" / "config" / "skills"
        alt_skill_root = (workspace_root / ".agents" / "skills") if workspace_root else None
        for sname in skill_names:
            cand = primary_skill_root / sname
            if not cand.is_dir() and alt_skill_root and (alt_skill_root / sname).is_dir():
                cand = alt_skill_root / sname
            if cand.is_dir():
                shutil.copytree(
                    cand,
                    tmp_skills / sname,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
                    dirs_exist_ok=True,
                )

        meta = {"version": current_ver, "savedAt": _now_iso(), "skills": skill_names}
        (tmp_backup / "backup_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if self.backup_dir.exists():
            shutil.rmtree(self.backup_dir, ignore_errors=True)
        tmp_backup.rename(self.backup_dir)

    def _copy_app_tree(self, src_app: Path) -> None:
        for fname in APP_MANAGED_FILES:
            src = src_app / fname
            if src.is_file():
                shutil.copy2(src, self.app_root / fname)
        if (self.app_root / "launch.sh").is_file():
            os.chmod(self.app_root / "launch.sh", 0o755)
        for dname in APP_MANAGED_DIRS:
            src_d = src_app / dname
            if src_d.is_dir():
                dst_d = self.app_root / dname
                shutil.copytree(
                    src_d,
                    dst_d,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
                    dirs_exist_ok=True,
                )

    def _sync_skills_dir(self, src_skills: Path, workspace_root: Path | None) -> int:
        if not src_skills.is_dir():
            return 0
        synced = 0
        target_roots = self._skill_target_roots(workspace_root)
        for skill_dir in src_skills.iterdir():
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            for t_root in target_roots:
                t_root.mkdir(parents=True, exist_ok=True)
                dst = t_root / skill_dir.name
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(
                    skill_dir,
                    dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
                )
            synced += 1
        return synced

    def _maybe_install_requirements(self, old_req_sha: str) -> None:
        new_req = self.app_root / "requirements.txt"
        new_sha = _file_sha256(new_req)
        if not new_sha or new_sha == old_req_sha:
            return
        uv_bin = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        if Path(uv_bin).is_file():
            subprocess.run(
                [uv_bin, "pip", "install", "--python", sys.executable, "-r", str(new_req)],
                capture_output=True,
                text=True,
                timeout=120,
            )

    def schedule_restart(self, *, port: int = 8787) -> None:
        launch_sh = self.app_root / "launch.sh"
        if not launch_sh.is_file():
            return
        cmd = (
            "/bin/sleep 0.6; "
            f"pkill -f 'agent_bridge.py.*--port {int(port)}' >/dev/null 2>&1 || true; "
            f"pkill -f 'uvicorn server:app.*--port {int(port)}' >/dev/null 2>&1 || true; "
            "/bin/sleep 0.5; "
            f"WASH_APP_PORT={int(port)} WASH_PYTHON_BIN='{sys.executable}' "
            f"/bin/zsh '{launch_sh}' --no-browser >/dev/null 2>&1"
        )
        subprocess.Popen(
            ["/bin/zsh", "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def apply_update(
        self,
        *,
        workspace_root: Path | None = None,
        port: int = 8787,
        auto_restart: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._applying:
                raise RuntimeError("正在执行热更新，请勿重复触发")
            self._applying = True

        try:
            cfg = self.get_config()
            local = self.get_local_manifest()
            old_ver = str(local.get("version") or "0.0.0")
            remote = self._fetch_remote_manifest(cfg)
            new_ver = str(remote.get("version") or "")
            old_req_sha = _file_sha256(self.app_root / "requirements.txt")

            with tempfile.TemporaryDirectory(prefix="wash_hotupdate_") as tmp:
                tmp_path = Path(tmp)
                zip_path = tmp_path / "update.zip"
                extract_dir = tmp_path / "unzipped"
                extract_dir.mkdir(parents=True, exist_ok=True)

                self._download_archive(cfg, remote, zip_path)
                _safe_extract_zip(zip_path, extract_dir)
                bundle_root = _locate_bundle_root(extract_dir)
                staged_app = bundle_root / "app"
                staged_skills = bundle_root / "skills"
                self._verify_staged_app(staged_app)

                # Write root version.json cleanly into staged app/version.json if needed
                staged_ver_file = bundle_root / "version.json"
                if staged_ver_file.is_file():
                    shutil.copy2(staged_ver_file, staged_app / "version.json")

                skill_names = (
                    [d.name for d in staged_skills.iterdir() if d.is_dir() and not d.name.startswith(".")]
                    if staged_skills.is_dir()
                    else []
                )
                self._create_backup(old_ver, skill_names, workspace_root)
                self._copy_app_tree(staged_app)
                synced_skills = self._sync_skills_dir(staged_skills, workspace_root)
                self._maybe_install_requirements(old_req_sha)

            self._cached_check = {
                "remote": remote,
                "checkedAt": _now_iso(),
                "error": "",
            }
            if auto_restart:
                self.schedule_restart(port=port)
            return {
                "ok": True,
                "previousVersion": old_ver,
                "newVersion": new_ver or self.current_version(),
                "changelog": str(remote.get("changelog") or ""),
                "syncedSkillsCount": synced_skills,
                "restarting": auto_restart,
            }
        finally:
            with self._lock:
                self._applying = False

    def rollback_update(
        self,
        *,
        workspace_root: Path | None = None,
        port: int = 8787,
        auto_restart: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._applying:
                raise RuntimeError("正在执行更新操作，请稍候")
            self._applying = True
        try:
            meta = self._backup_meta()
            bak_app = self.backup_dir / "app"
            bak_skills = self.backup_dir / "skills"
            if not meta.get("version") or not bak_app.is_dir():
                raise RuntimeError("当前没有可回滚的上一版本备份")
            self._verify_staged_app(bak_app)
            cur_ver = self.current_version()
            self._copy_app_tree(bak_app)
            synced = self._sync_skills_dir(bak_skills, workspace_root)
            self._cached_check = None
            if auto_restart:
                self.schedule_restart(port=port)
            return {
                "ok": True,
                "previousVersion": cur_ver,
                "restoredVersion": str(meta["version"]),
                "syncedSkillsCount": synced,
                "restarting": auto_restart,
            }
        finally:
            with self._lock:
                self._applying = False


update_manager = HotUpdateManager()
