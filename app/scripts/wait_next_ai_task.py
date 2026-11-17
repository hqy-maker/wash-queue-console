#!/usr/bin/env python3
"""Maintain desktop coordinator heartbeat and wait for the next AI shot task."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _http_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _superseded_result(event_seq: int = 0) -> dict[str, Any]:
    return {
        "ok": True,
        "timedOut": False,
        "superseded": True,
        "eventSeq": event_seq,
        "aiPendingCount": 0,
        "note": "已被新开启的桌面主控对话接替，当前旧监听自动退出，请勿再重启 wait_next_ai_task.py",
    }


def _send_desktop_heartbeat(
    base_url: str,
    watcher_id: str,
    ttl_sec: float = 45.0,
    *,
    watcher_token: str = "",
    replace_token: bool = False,
) -> bool:
    try:
        _http_json(
            base_url,
            "POST",
            "/api/agent/heartbeat",
            {
                "watcherId": watcher_id,
                "mode": "desktop",
                "busy": False,
                "activeShotIds": [],
                "pid": os.getpid(),
                "ttlSec": ttl_sec,
                "watcherToken": watcher_token,
                "replaceToken": replace_token,
            },
            timeout=5.0,
        )
        return True
    except Exception:
        return False


def _is_superseded(base_url: str, watcher_token: str) -> bool:
    if not watcher_token:
        return False
    try:
        st = _http_json(base_url, "GET", "/api/agent/status", timeout=4.0)
        active_tok = str(st.get("desktopWatcherToken") or "").strip()
        return bool(active_tok and active_tok != watcher_token)
    except Exception:
        return False


def _collect_ai_pending(pending_data: dict[str, Any]) -> list[dict[str, Any]]:
    mode = str(pending_data.get("aiDispatchMode") or "desktop_subagent").strip()
    if mode == "cli_bridge":
        return []
    ai_ids = {str(x) for x in (pending_data.get("aiPendingShotIds") or [])}
    shots = [
        s
        for s in (pending_data.get("pending") or [])
        if str(s.get("id") or "") in ai_ids
    ]
    return [
        {
            "id": s.get("id"),
            "shotCode": s.get("shotCode"),
            "title": s.get("title"),
            "status": s.get("status"),
            "platform": s.get("platform"),
            "workMode": s.get("workMode"),
            "targetDuration": s.get("targetDuration"),
            "desktopConversationId": s.get("desktopConversationId") or "",
            "canResumeDesktopSubagent": bool(s.get("canResumeDesktopSubagent")),
        }
        for s in shots
    ]


def wait_for_next_ai_task(
    base_url: str,
    *,
    watcher_id: str = "desktop_coordinator",
    total_timeout: float = 3600.0,
    poll_timeout: float = 25.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(5.0, float(total_timeout))
    since_seq = 0
    pid = os.getpid()
    watcher_token = f"{pid}_{int(time.time() * 1000)}"
    need_initial_hb = not _send_desktop_heartbeat(
        base_url,
        watcher_id,
        ttl_sec=poll_timeout + 25.0,
        watcher_token=watcher_token,
        replace_token=True,
    )

    while True:
        try:
            if need_initial_hb:
                need_initial_hb = not _send_desktop_heartbeat(
                    base_url,
                    watcher_id,
                    ttl_sec=poll_timeout + 25.0,
                    watcher_token=watcher_token,
                    replace_token=True,
                )
            pending_data = _http_json(base_url, "GET", "/api/agent/pending", timeout=6.0)
            since_seq = max(since_seq, int(pending_data.get("eventSeq") or 0))
            ai_shots = _collect_ai_pending(pending_data)
            if ai_shots:
                if _is_superseded(base_url, watcher_token):
                    return _superseded_result(since_seq)
                _send_desktop_heartbeat(
                    base_url,
                    watcher_id,
                    ttl_sec=180.0,
                    watcher_token=watcher_token,
                    replace_token=False,
                )
                return {
                    "ok": True,
                    "timedOut": False,
                    "eventSeq": since_seq,
                    "aiPendingCount": len(ai_shots),
                    "aiPendingShotIds": [s["id"] for s in ai_shots],
                    "aiShots": ai_shots,
                }
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            need_initial_hb = True
            if time.monotonic() >= deadline:
                return {"ok": True, "timedOut": True, "aiPendingCount": 0, "reason": "server_unreachable"}
            time.sleep(1.0)
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ok": True, "timedOut": True, "eventSeq": since_seq, "aiPendingCount": 0}

        wait_sec = min(max(1.0, poll_timeout), remaining)
        qs = urllib.parse.urlencode(
            {
                "since": str(since_seq),
                "timeout": f"{wait_sec:.1f}",
                "actionable": "true",
                "ai_only": "true",
                "watcher": watcher_id,
                "mode": "desktop",
                "watcher_token": watcher_token,
                "pid": str(pid),
            }
        )
        try:
            res = _http_json(
                base_url,
                "GET",
                f"/api/events/wait?{qs}",
                timeout=wait_sec + 10.0,
            )
            if res.get("superseded"):
                return _superseded_result(int(res.get("eventSeq") or since_seq))
            since_seq = max(since_seq, int(res.get("eventSeq") or since_seq))
            if not res.get("timedOut") and res.get("pending"):
                pending_data = _http_json(base_url, "GET", "/api/agent/pending", timeout=6.0)
                ai_shots = _collect_ai_pending(pending_data)
                if ai_shots:
                    if _is_superseded(base_url, watcher_token):
                        return _superseded_result(since_seq)
                    _send_desktop_heartbeat(
                        base_url,
                        watcher_id,
                        ttl_sec=180.0,
                        watcher_token=watcher_token,
                        replace_token=False,
                    )
                    return {
                        "ok": True,
                        "timedOut": False,
                        "eventSeq": since_seq,
                        "aiPendingCount": len(ai_shots),
                        "aiPendingShotIds": [s["id"] for s in ai_shots],
                        "aiShots": ai_shots,
                    }
                time.sleep(0.5)
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            need_initial_hb = True
            if time.monotonic() >= deadline:
                return {"ok": True, "timedOut": True, "eventSeq": since_seq, "aiPendingCount": 0}
            time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for next Wash Queue Console AI task")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("WASH_APP_URL", "http://127.0.0.1:8787"),
        help="Console server base URL",
    )
    parser.add_argument("--watcher-id", default="desktop_coordinator")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--poll-timeout", type=float, default=25.0)
    args = parser.parse_args()

    result = wait_for_next_ai_task(
        args.base_url,
        watcher_id=args.watcher_id,
        total_timeout=args.timeout,
        poll_timeout=args.poll_timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
