#!/usr/bin/env python3
"""Deterministic Chrome DevTools CDP automation for Dreamina 720P depth capture.

Acquires the short `chrome_ui` mutex lock, attaches to the live Dreamina Canvas
Chrome tab via `DevToolsActivePort`, triggers `深度动作捕捉 -> 720P -> 生成` on
the specified `sourceNodeId`, discovers the newly created depth node ID via
`dreamina-canvas node find` diff, and releases the lock immediately (~4-6s).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def _find_devtools_port_file() -> Path | None:
    env_path = os.environ.get("CHROME_DEVTOOLS_ACTIVE_PORT", "").strip()
    candidates = [
        Path(env_path).expanduser() if env_path else None,
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "DevToolsActivePort",
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome Canary" / "DevToolsActivePort",
    ]
    for p in candidates:
        if p is not None and p.is_file():
            return p
    return None


class _CdpClient:
    """Minimal stdlib RFC-6455 WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, host: str, port: int, ws_path: str, timeout: float = 12.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {ws_path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(req.encode("utf-8"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("Chrome CDP WebSocket handshake closed")
            resp += chunk
        status_line = resp.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        if "101" not in status_line:
            raise RuntimeError(f"Chrome CDP handshake rejected: {status_line}")
        self._next_id = 1

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        hdr = bytearray([0x80 | (opcode & 0x0F)])
        ln = len(payload)
        if ln < 126:
            hdr.append(0x80 | ln)
        elif ln < 65536:
            hdr.append(0x80 | 126)
            hdr.extend(struct.pack("!H", ln))
        else:
            hdr.append(0x80 | 127)
            hdr.extend(struct.pack("!Q", ln))
        hdr.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(hdr) + masked)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("Chrome CDP socket EOF")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_message(self) -> dict[str, Any]:
        chunks: list[bytes] = []
        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            op = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if (b2 & 0x80) else b""
            data = self._read_exact(ln)
            if mask_key:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                raise EOFError("Chrome CDP closed connection")
            if op == 0x9:
                self._send_frame(0xA, data)
                continue
            if op == 0xA:
                continue
            if op in {0x1, 0x2, 0x0}:
                chunks.append(data)
                if fin:
                    return json.loads(b"".join(chunks).decode("utf-8"))

    def call(self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None) -> dict[str, Any]:
        cid = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"id": cid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._send_frame(0x1, json.dumps(msg).encode("utf-8"))
        while True:
            reply = self._recv_message()
            if reply.get("id") == cid:
                if "error" in reply and reply["error"]:
                    raise RuntimeError(f"CDP {method} failed: {reply['error']}")
                return reply.get("result") or {}


def _run_dreamina_json(args: list[str], timeout: int = 30) -> dict[str, Any]:
    cmd = ["dreamina-canvas", "--non-interactive", "--format", "json", *args]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    start = out.find("{")
    end = out.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"dreamina-canvas bad output rc={proc.returncode}: {out[:300]}")
    payload = json.loads(out[start : end + 1])
    if proc.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(f"dreamina-canvas error: {json.dumps(payload, ensure_ascii=False)[:300]}")
    return payload


def _list_all_canvas_items(project_uuid: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    for _ in range(8):
        res = _run_dreamina_json(["node", "find", "--project-id", project_uuid, "--limit", "50", "--offset", str(offset)], timeout=20)
        data = res.get("data") or {}
        chunk = data.get("items") or data.get("nodes") or []
        items.extend(x for x in chunk if isinstance(x, dict))
        if not data.get("hasMore") or not chunk:
            break
        offset += len(chunk)
    return items


def find_existing_depth_node(
    project_uuid: str,
    shot_id: str,
    source_node_id: str,
    *,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return existing depth node summary if already created for `shot_id` or `source_node_id`."""
    all_items = items if items is not None else _list_all_canvas_items(project_uuid)
    cand_ids: list[str] = []
    exact_match: dict[str, Any] | None = None
    for it in all_items:
        nid = str(it.get("nodeId") or "").strip()
        ntype = str(it.get("type") or "").strip()
        title = str(it.get("title") or "").strip()
        if not nid or ntype != "video":
            continue
        if title.startswith(f"{shot_id}-原片_深度") or title.startswith(f"{shot_id}-深度"):
            exact_match = it
            cand_ids.insert(0, nid)
        elif "深度动作捕捉" in title and nid not in cand_ids and source_node_id:
            cand_ids.append(nid)

    if source_node_id and cand_ids:
        for i in range(0, len(cand_ids), 20):
            batch = cand_ids[i : i + 20]
            cmd = ["node", "show", "--project-id", project_uuid]
            for cid in batch:
                cmd.extend(["--node-id", cid])
            try:
                shown = _run_dreamina_json(cmd, timeout=25)
                for wrap in (shown.get("data") or {}).get("nodes") or []:
                    node = wrap.get("node") if isinstance(wrap, dict) else None
                    if not isinstance(node, dict):
                        continue
                    up = [str(u) for u in (node.get("upstreamNodeIds") or [])]
                    if source_node_id in up:
                        return node
            except Exception:
                pass
    return exact_match


def _build_trigger_js(shot_id: str, source_node_id: str) -> str:
    shot_json = json.dumps(shot_id, ensure_ascii=False)
    src_json = json.dumps(source_node_id, ensure_ascii=False)
    return f"""
async () => {{
  const SHOT_ID = {shot_json};
  const SRC_ID = {src_json};
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();

  const isInViewport = (el) => {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && r.right > 40 && r.left < window.innerWidth - 40 && r.bottom > 40 && r.top < window.innerHeight - 40;
  }};

  const tryCenterViaSearch = async () => {{
    const findSearchInput = () => [...document.querySelectorAll("input")].find((inp) => {{
      const rect = inp.getBoundingClientRect();
      return rect.width > 40 && rect.height > 12;
    }});
    let input = findSearchInput();
    if (!input) {{
      const searchBtn = [...document.querySelectorAll("button, [role='button']")].find((b) => {{
        const br = b.getBoundingClientRect();
        if (br.width <= 0 || br.height <= 0) return false;
        const txt = norm(b.getAttribute("aria-label") || b.innerText);
        return txt.includes("搜索");
      }});
      if (!searchBtn) return false;
      searchBtn.click();
      for (let i = 0; i < 6 && !input; i++) {{
        await sleep(150);
        input = findSearchInput();
      }}
    }}
    if (input) {{
      if (input._valueTracker && typeof input._valueTracker.setValue === "function") {{
        input._valueTracker.setValue("");
      }}
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
      if (setter) setter.call(input, SHOT_ID + "-原片");
      else input.value = SHOT_ID + "-原片";
      input.dispatchEvent(new Event("input", {{ bubbles: true }}));
      input.dispatchEvent(new Event("change", {{ bubbles: true }}));
      await sleep(450);
    }}
    const hit = [...document.querySelectorAll("button, [role='button'], [role='option'], li, div")].find((el) => {{
      const rect = el.getBoundingClientRect();
      if (rect.width < 40 || rect.height < 16 || rect.height > 90) return false;
      const t = norm(el.getAttribute("aria-label") || el.innerText);
      return t === SHOT_ID + "-原片" || (t.includes(SHOT_ID + "-原片") && !t.includes("深度"));
    }});
    if (hit) {{
      hit.click();
      await sleep(550);
      return true;
    }}
    const fallbackHit = [...document.querySelectorAll("button, [role='button'], div")].find((el) => {{
      const rect = el.getBoundingClientRect();
      if (rect.width < 40 || rect.height < 16 || rect.height > 90) return false;
      const t = norm(el.getAttribute("aria-label") || el.innerText);
      return t.includes(SHOT_ID + "-构图") && !t.includes("深度");
    }});
    if (fallbackHit) {{
      fallbackHit.click();
      await sleep(550);
      return true;
    }}
    return false;
  }};

  let srcEl = document.querySelector(`[data-id="${{SRC_ID}}"]`);
  if (!isInViewport(srcEl)) {{
    await tryCenterViaSearch();
    srcEl = document.querySelector(`[data-id="${{SRC_ID}}"]`);
  }}
  if (!srcEl) {{
    return {{ ok: false, needsReload: true, reason: "source_node_not_mounted" }};
  }}

  const r = srcEl.getBoundingClientRect();
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {{
    srcEl.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window }}));
  }}

  let depthBtn = null;
  for (let i = 0; i < 12; i++) {{
    await sleep(220);
    depthBtn = [...document.querySelectorAll("button")].find((b) => {{
      const t = norm(b.innerText || b.getAttribute("aria-label"));
      return !b.disabled && (t === "深度动作捕捉" || t.includes("深度动作捕捉"));
    }});
    if (depthBtn) break;
  }}
  if (!depthBtn) {{
    return {{ ok: false, reason: "depth_toolbar_button_not_found" }};
  }}
  depthBtn.click();

  let bGen = null;
  let clicked720 = false;
  for (let i = 0; i < 15; i++) {{
    await sleep(220);
    if (!clicked720) {{
      const b720 = [...document.querySelectorAll("button")].find((b) => norm(b.innerText) === "720P");
      if (b720) {{
        b720.click();
        clicked720 = true;
        await sleep(200);
      }}
    }}
    bGen = [...document.querySelectorAll("button")].find((b) => {{
      const br = b.getBoundingClientRect();
      return br.width > 20 && br.height > 16 && !b.disabled && norm(b.innerText) === "生成";
    }});
    if (bGen) break;
  }}
  if (!bGen) {{
    return {{ ok: false, reason: "depth_generate_button_not_found", clicked720 }};
  }}
  bGen.click();
  await sleep(1200);

  const domDepthIds = [...document.querySelectorAll("[data-id]")].filter((el) => {{
    const t = norm(el.innerText || el.getAttribute("aria-label"));
    return t.includes(SHOT_ID + "-原片_深度动作捕捉") || t.includes("深度动作捕捉");
  }}).map((el) => el.getAttribute("data-id"));

  return {{
    ok: true,
    clicked720,
    clickedGenerate: true,
    domDepthIdsTail: domDepthIds.slice(-4),
  }};
}}
"""


def trigger_dreamina_depth_capture(
    project_uuid: str,
    shot_id: str,
    source_node_id: str,
    *,
    canvas_url: str = "",
    base_url: str = "http://127.0.0.1:8787",
    existing_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministically click 720P 深度动作捕捉 in Chrome under `chrome_ui` lock."""
    t0 = time.monotonic()
    clean_pid = (project_uuid or "").strip()
    clean_sid = (shot_id or "").strip()
    clean_src = (source_node_id or "").strip()
    if not clean_pid or not clean_sid or not clean_src:
        return {"ok": False, "reason": "missing_project_or_source_node"}

    existing = find_existing_depth_node(clean_pid, clean_sid, clean_src, items=existing_items)
    if existing and existing.get("nodeId"):
        return {
            "ok": True,
            "depthNodeId": str(existing["nodeId"]),
            "alreadyExisted": True,
            "elapsedSec": round(time.monotonic() - t0, 2),
        }

    port_file = _find_devtools_port_file()
    if port_file is None:
        return {"ok": False, "reason": "chrome_devtools_port_unavailable"}

    lock_token = ""
    if base_url:
        try:
            with httpx.Client(base_url=base_url, timeout=50.0) as client:
                r = client.post(
                    "/api/agent/locks/chrome_ui/acquire",
                    json={"owner": clean_sid, "ttlSec": 45.0, "waitSec": 40.0},
                )
                if r.status_code != 200 or not r.json().get("acquired"):
                    return {"ok": False, "reason": f"chrome_ui_lock_busy ({r.status_code})"}
                lock_token = str(r.json().get("token") or "")
        except Exception as exc:
            return {"ok": False, "reason": f"chrome_ui_lock_error: {exc}"}

    cdp: _CdpClient | None = None
    try:
        before_items = _list_all_canvas_items(clean_pid)
        rechecked = find_existing_depth_node(clean_pid, clean_sid, clean_src, items=before_items)
        if rechecked and rechecked.get("nodeId"):
            return {
                "ok": True,
                "depthNodeId": str(rechecked["nodeId"]),
                "alreadyExisted": True,
                "elapsedSec": round(time.monotonic() - t0, 2),
            }
        before_ids = {str(it.get("nodeId") or "") for it in before_items if it.get("nodeId")}

        port_str, ws_path = port_file.read_text(encoding="utf-8").strip().splitlines()[:2]
        cdp = _CdpClient("127.0.0.1", int(port_str), ws_path, timeout=18.0)

        targets = cdp.call("Target.getTargets").get("targetInfos") or []
        page = next(
            (t for t in targets if t.get("type") == "page" and clean_pid in str(t.get("url") or "")),
            None,
        )
        if page is None:
            target_url = (canvas_url or "").strip() or f"https://jimeng.jianying.com/ai-tool/ai-canvas/{clean_pid}"
            created = cdp.call("Target.createTarget", {"url": target_url})
            target_id = str(created.get("targetId") or "")
            if not target_id:
                return {"ok": False, "reason": "failed_to_open_canvas_tab"}
            time.sleep(3.5)
        else:
            target_id = str(page["targetId"])

        att = cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sess_id = str(att.get("sessionId") or "")
        if not sess_id:
            return {"ok": False, "reason": "cdp_attach_no_session"}

        cdp.call("Page.bringToFront", session_id=sess_id)
        js_code = f"({_build_trigger_js(clean_sid, clean_src)})()"
        ev = cdp.call(
            "Runtime.evaluate",
            {"expression": js_code, "awaitPromise": True, "returnByValue": True},
            session_id=sess_id,
        )
        res_val = ((ev.get("result") or {}).get("value")) or {}
        if isinstance(res_val, dict) and res_val.get("needsReload"):
            cdp.call("Page.reload", {"ignoreCache": False}, session_id=sess_id)
            deadline_ready = time.monotonic() + 8.0
            while time.monotonic() < deadline_ready:
                time.sleep(0.8)
                chk = cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": f"Boolean(document.querySelector('[data-id=\"{clean_src}\"]') || [...document.querySelectorAll('button')].some(b => (b.getAttribute('aria-label')||b.innerText||'').includes('搜索')))",
                        "returnByValue": True,
                    },
                    session_id=sess_id,
                )
                if (chk.get("result") or {}).get("value"):
                    break
            ev2 = cdp.call(
                "Runtime.evaluate",
                {"expression": js_code, "awaitPromise": True, "returnByValue": True},
                session_id=sess_id,
            )
            res_val = ((ev2.get("result") or {}).get("value")) or {}

        if not isinstance(res_val, dict) or not res_val.get("ok"):
            return {
                "ok": False,
                "reason": str((res_val or {}).get("reason") or "cdp_js_trigger_failed"),
                "detail": res_val,
                "elapsedSec": round(time.monotonic() - t0, 2),
            }

        dom_tail = [str(x).strip() for x in (res_val.get("domDepthIdsTail") or []) if str(x).strip() and str(x).strip() not in before_ids]
        depth_node_id = ""
        for _ in range(6):
            after_items = _list_all_canvas_items(clean_pid)
            new_vids = [
                it
                for it in after_items
                if str(it.get("nodeId") or "") not in before_ids and it.get("type") == "video"
            ]
            cand_new_ids = [str(v["nodeId"]) for v in new_vids if v.get("nodeId")]
            for did in dom_tail:
                if did not in cand_new_ids:
                    cand_new_ids.append(did)
            if cand_new_ids:
                # Prefer exact upstream match
                cmd = ["node", "show", "--project-id", clean_pid]
                for cid in cand_new_ids:
                    cmd.extend(["--node-id", cid])
                try:
                    shown = _run_dreamina_json(cmd, timeout=15)
                    for wrap in (shown.get("data") or {}).get("nodes") or []:
                        node = wrap.get("node") if isinstance(wrap, dict) else None
                        if isinstance(node, dict) and clean_src in [str(u) for u in (node.get("upstreamNodeIds") or [])]:
                            depth_node_id = str(node.get("nodeId") or "")
                            break
                except Exception:
                    pass
                if not depth_node_id and new_vids:
                    exact = next((v for v in new_vids if "深度" in str(v.get("title") or "")), None)
                    depth_node_id = str((exact or new_vids[0]).get("nodeId") or "")
                if depth_node_id:
                    break
            time.sleep(0.8)

        if not depth_node_id:
            found = find_existing_depth_node(clean_pid, clean_sid, clean_src)
            depth_node_id = str((found or {}).get("nodeId") or "")

        return {
            "ok": bool(depth_node_id),
            "depthNodeId": depth_node_id,
            "clicked720": bool(res_val.get("clicked720")),
            "alreadyExisted": False,
            "reason": "" if depth_node_id else "triggered_but_depth_node_id_unconfirmed",
            "elapsedSec": round(time.monotonic() - t0, 2),
        }
    except Exception as exc:
        return {"ok": False, "reason": f"cdp_exception: {exc}", "elapsedSec": round(time.monotonic() - t0, 2)}
    finally:
        if cdp is not None:
            cdp.close()
        if base_url and lock_token:
            try:
                with httpx.Client(base_url=base_url, timeout=6.0) as client:
                    client.post("/api/agent/locks/chrome_ui/release", json={"token": lock_token})
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger Dreamina 720P depth capture via Chrome CDP")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--shot-id", required=True)
    parser.add_argument("--source-node-id", required=True)
    parser.add_argument("--canvas-url", default="")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    res = trigger_dreamina_depth_capture(
        args.project_id,
        args.shot_id,
        args.source_node_id,
        canvas_url=args.canvas_url,
        base_url=args.base_url,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
