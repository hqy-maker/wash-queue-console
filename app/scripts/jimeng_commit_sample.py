#!/usr/bin/env python3
"""Atomic Dreamina sample-node commit helper: create/edit sample node + quote + callback in 1 command."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

_CODE_FENCE_RE = re.compile(r"```(?:text|prompt|markdown)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_prompt_from_md(prompt_file: Path) -> str:
    raw = prompt_file.read_text(encoding="utf-8")
    m = _CODE_FENCE_RE.search(raw)
    text = (m.group(1) if m else raw).strip()
    if not text:
        raise ValueError(f"提示词文件正文为空: {prompt_file}")
    return text


def _run_dreamina(args: list[str], timeout: int = 45) -> dict[str, Any]:
    cmd = ["dreamina-canvas", "--non-interactive", "--format", "json", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    raw = (proc.stdout or proc.stderr or "").strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dreamina-canvas 非 JSON 返回 (exit={proc.returncode}): {raw[:300]}") from exc
    if proc.returncode != 0 or (isinstance(parsed, dict) and parsed.get("ok") is False):
        err = parsed.get("error") if isinstance(parsed, dict) else {}
        raise RuntimeError(f"dreamina-canvas 错误 (exit={proc.returncode}): {json.dumps(err or raw, ensure_ascii=False)[:400]}")
    return parsed if isinstance(parsed, dict) else {}


def _extract_node_id(resp: dict[str, Any]) -> str:
    data = resp.get("data") or {}
    if isinstance(data.get("node"), dict):
        nid = str(data["node"].get("nodeId") or "").strip()
        if nid:
            return nid
    items = data.get("items") or []
    if isinstance(items, list) and items and isinstance(items[0], dict):
        nid = str(items[0].get("nodeId") or "").strip()
        if nid:
            return nid
    return str(data.get("nodeId") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Create/edit Dreamina video sample node, quote credits, and update Wash Queue Console")
    ap.add_argument("--shot-id", required=True)
    ap.add_argument("--claim-token", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--mode", default="m2v", choices=["m2v", "t2v", "first_last_frame"])
    ap.add_argument("--model", default="seedance_2.5_draft")
    ap.add_argument("--ratio", default="21:9")
    ap.add_argument("--resolution", default="480p")
    ap.add_argument("--duration", required=True, type=float)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--x", type=float, default=None)
    ap.add_argument("--y", type=float, default=0.0)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--sample-node-id", default="")
    ap.add_argument("--revision-direction", default="")
    ap.add_argument("--summary", default="")
    ap.add_argument("--masked-comp-path", default="")
    ap.add_argument("--whiteout-json", default="")
    ap.add_argument("--conversation-id", default="")
    ap.add_argument("--resolve-feedbacks", action="store_true")
    ap.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = ap.parse_args()

    prompt_path = Path(args.prompt_file).expanduser().resolve()
    prompt_text = _extract_prompt_from_md(prompt_path)
    dur_sec = max(4, min(30, int(round(float(args.duration)))))
    gen_x = int(round(args.x if args.x is not None else (800.0 if args.mode == "t2v" else 3620.0)))
    gen_y = int(round(args.y))

    existing_sample = args.sample_node_id.strip()
    if existing_sample.startswith("node_"):
        cmd = [
            "node", "edit", "video",
            "--project-id", args.project_id,
            "--node-id", existing_sample,
            "--mode", args.mode,
            "--model", args.model,
            "--ratio", args.ratio,
            "--resolution", args.resolution,
            "--duration", str(dur_sec),
            "--count", str(max(1, args.count)),
            "--prompt", prompt_text,
        ]
    else:
        cmd = [
            "node", "create", "video",
            "--project-id", args.project_id,
            "--title", f"{args.shot_id}-样片",
            "--model", args.model,
            "--mode", args.mode,
            "--ratio", args.ratio,
            "--resolution", args.resolution,
            "--duration", str(dur_sec),
            "--count", str(max(1, args.count)),
            "--x", str(gen_x),
            "--y", str(gen_y),
            "--prompt", prompt_text,
        ]
    for r in args.ref:
        clean_ref = str(r or "").strip()
        if clean_ref:
            cmd.extend(["--ref", clean_ref])

    res = _run_dreamina(cmd, timeout=45)
    sample_id = existing_sample or _extract_node_id(res)
    if not sample_id:
        raise RuntimeError(f"创建样片节点未返回 nodeId: {json.dumps(res, ensure_ascii=False)[:300]}")
    draft_ver = (res.get("data") or {}).get("draftVersion")

    quoted_max = None
    conf_req = None
    quote_warn = ""
    try:
        q_res = _run_dreamina(
            ["node", "quote", "--project-id", args.project_id, "--node-id", sample_id],
            timeout=25,
        )
        qd = q_res.get("data") or {}
        quoted_max = qd.get("totalMaxCredits")
        if quoted_max is None and isinstance(qd.get("items"), list) and qd["items"]:
            quoted_max = (qd["items"][0] or {}).get("maxCredits")
        conf_req = qd.get("confirmationRequired")
        if qd.get("draftVersion") is not None:
            draft_ver = qd["draftVersion"]
    except Exception as exc:
        quote_warn = str(exc)

    jm_patch: dict[str, Any] = {
        "platform": "jimeng",
        "projectId": args.project_id,
        "sampleNodeId": sample_id,
        "mode": args.mode,
        "model": args.model,
        "ratio": args.ratio,
        "resolution": args.resolution,
        "duration": dur_sec,
        "confirmationState": "quoted" if quoted_max is not None else "created",
    }
    if draft_ver is not None:
        jm_patch["draftVersion"] = draft_ver
    if quoted_max is not None:
        jm_patch["quotedMaxCredits"] = quoted_max
    if conf_req is not None:
        jm_patch["confirmationRequired"] = bool(conf_req)
    if args.masked_comp_path.strip():
        jm_patch["maskedCompPath"] = args.masked_comp_path.strip()

    rev_dir = args.revision_direction.strip() or (
        f"已撰写提示词并在即梦画布创建 {dur_sec}s {args.mode} 样片节点 {sample_id}（报价 {quoted_max or '-'} 积分，未扣费生成）。"
    )
    summary = args.summary.strip() or f"已建样片 {sample_id}（{dur_sec}s / {args.mode}，预报价 {quoted_max or '-'} 积分）"

    body: dict[str, Any] = {
        "claimToken": args.claim_token,
        "status": "ready_on_canvas",
        "promptText": prompt_text,
        "promptFilePath": str(prompt_path),
        "libtvSampleNodeKey": sample_id,
        "jimengMeta": jm_patch,
        "revisionDirection": rev_dir,
        "revisionSummary": summary,
        "agentSummary": summary,
    }
    if args.resolve_feedbacks:
        body["resolveFeedbacks"] = True
    if args.conversation_id.strip():
        body["conversationId"] = args.conversation_id.strip()
    if args.whiteout_json.strip():
        body["whiteoutUpdates"] = json.loads(args.whiteout_json)

    with httpx.Client(base_url=args.base_url, timeout=15.0) as client:
        cb = client.post(f"/api/shots/{args.shot_id}/agent-update", json=body)
        if cb.status_code == 409:
            print(json.dumps({"ok": True, "conflict": True, "sampleNodeId": sample_id}, ensure_ascii=False))
            return 0
        cb.raise_for_status()

    out = {
        "ok": True,
        "shotId": args.shot_id,
        "sampleNodeId": sample_id,
        "draftVersion": draft_ver,
        "quotedMaxCredits": quoted_max,
        "duration": dur_sec,
        "mode": args.mode,
    }
    if quote_warn:
        out["quoteWarning"] = quote_warn
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
