#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from store import QUEUE_PATH

ACTIONABLE_STATUSES = {
    "queued",
    "run_requested",
    "prompt_update_requested",
    "feedback_queued",
    "final_requested",
    "recheck_requested",
}


def summarize_pending(payload: dict) -> dict:
    is_claimed = "claimed" in payload
    shots = payload.get("claimed", []) if is_claimed else payload.get("pending", [])
    summary_items = []
    for s in shots:
        fbs = [f for f in s.get("feedbacks", []) if f.get("status") == "pending"]
        latest_fb = fbs[-1] if fbs else None
        last_conf = (s.get("confirmationHistory") or [])[-1] if s.get("confirmationHistory") else None
        summary_items.append(
            {
                "id": s["id"],
                "scene": s.get("scene", ""),
                "status": s["status"],
                "processingTaskType": s.get("processingTaskType", ""),
                "claimToken": s.get("claimToken", ""),
                "claimedBy": s.get("claimedBy", ""),
                "platform": s.get("platform", "libtv"),
                "projectUuid": s.get("projectUuid"),
                "projectName": s.get("projectName"),
                "canvasUrl": s.get("canvasUrl", ""),
                "notes": s.get("notes", ""),
                "userPrompt": s.get("userPrompt", ""),
                "promptText": s.get("promptText", ""),
                "previousPromptText": s.get("previousPromptText", ""),
                "pendingPromptEdit": s.get("pendingPromptEdit", ""),
                "promptUpdatePrevStatus": s.get("promptUpdatePrevStatus", ""),
                "whiteoutOptions": s.get("whiteoutOptions"),
                "libtv": s.get("libtv"),
                "jimengMeta": s.get("jimengMeta") or {},
                "sourceVideo": (s.get("sourceVideo") or {}).get("filePath"),
                "compImage": (s.get("compImage") or {}).get("filePath"),
                "charRefs": [
                    {
                        "slotId": c.get("slotId"),
                        "label": c.get("label"),
                        "filePath": c.get("filePath"),
                        "libtvNodeKey": c.get("libtvNodeKey"),
                    }
                    for c in s.get("charRefs", [])
                ],
                "latestFeedback": latest_fb,
                "latestConfirmation": last_conf,
            }
        )
    out = {
        "eventSeq": payload.get("eventSeq"),
        "pendingCount": len(summary_items),
        "processingCount": payload.get("processingCount", 0),
        "processingShotIds": payload.get("processingShotIds", []),
        "digestMarkdown": payload.get("digestMarkdown", ""),
    }
    if is_claimed:
        out["claimedCount"] = len(summary_items)
        out["remainingPendingCount"] = payload.get("remainingPendingCount", 0)
        out["claimed"] = summary_items
    else:
        out["pending"] = summary_items
    return out


def check_local_queue() -> dict | None:
    if not QUEUE_PATH.is_file():
        return None
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        pending = [s for s in data.get("shots", []) if s.get("status") in ACTIONABLE_STATUSES]
        if pending:
            return {"eventSeq": data.get("eventSeq", 1), "pending": pending, "digestMarkdown": ""}
    except Exception:
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Event listener for SHZ Wash Queue Console")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WASH_APP_PORT", "8787")))
    parser.add_argument("--wait", action="store_true", help="Block until actionable shot/feedback arrives")
    parser.add_argument("--check", action="store_true", help="Check once and exit immediately")
    parser.add_argument("--claim", action="store_true", help="Atomically claim actionable shots as processing before exiting")
    parser.add_argument("--limit", type=int, default=10, help="Max pending shots to claim at once (default: 10)")
    parser.add_argument("--worker", default="coordinator", help="Worker identifier recorded when claiming shots")
    parser.add_argument("--mode", default="interactive", choices=["interactive", "bridge"], help="Watcher connection mode")
    parser.add_argument("--watcher", default="interactive_coordinator", help="Heartbeat watcher identifier")
    parser.add_argument("--timeout", type=float, default=20.0, help="Long-poll interval per HTTP call")
    args = parser.parse_args()

    base_url = f"http://127.0.0.1:{args.port}"

    def _print_and_exit(client: httpx.Client, payload: dict) -> int:
        if args.claim:
            claim_resp = client.post(
                "/api/agent/claim",
                json={"workerId": args.worker, "limit": args.limit},
            )
            claim_resp.raise_for_status()
            claimed_payload = claim_resp.json()
            if claimed_payload.get("claimedCount", 0) > 0:
                print(json.dumps(summarize_pending(claimed_payload), ensure_ascii=False, indent=2), flush=True)
                return 0
            return 2
        if payload.get("pendingCount", 0) > 0 or payload.get("pending"):
            print(json.dumps(summarize_pending(payload), ensure_ascii=False, indent=2), flush=True)
            return 0
        return 2

    with httpx.Client(base_url=base_url, timeout=args.timeout + 10.0) as client:
        if args.check or not args.wait:
            try:
                if args.claim:
                    return _print_and_exit(client, {})
                resp = client.get("/api/agent/pending")
                resp.raise_for_status()
                payload = resp.json()
                return _print_and_exit(client, payload)
            except Exception:
                local = check_local_queue()
                if local:
                    print(json.dumps(summarize_pending(local), ensure_ascii=False, indent=2), flush=True)
                    return 0
                return 2

        try:
            client.post(
                "/api/agent/heartbeat",
                json={
                    "watcherId": args.watcher,
                    "mode": args.mode,
                    "pid": os.getpid(),
                    "ttlSec": max(float(args.timeout or 20.0) + 15.0, 35.0),
                },
            )
        except Exception:
            pass

        since_seq = 0
        while True:
            try:
                resp = client.get(
                    "/api/events/wait",
                    params={
                        "since": since_seq,
                        "timeout": args.timeout,
                        "actionable": "true",
                        "watcher": args.watcher,
                        "mode": args.mode,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
                since_seq = int(payload.get("eventSeq", since_seq))
                if not payload.get("timedOut") and payload.get("pending"):
                    code = _print_and_exit(client, payload)
                    if code == 0:
                        return 0
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                local = check_local_queue()
                if local:
                    print(json.dumps(summarize_pending(local), ensure_ascii=False, indent=2), flush=True)
                    return 0
                time.sleep(2.0)


if __name__ == "__main__":
    raise SystemExit(main())
