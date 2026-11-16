#!/usr/bin/env python3
"""Deterministic local pre-preparation and fast-path sync helpers for agent_bridge.

Design goals:
1. Offload mechanical I/O & analysis from LLM workers (keyframe grid, audio RMS scan,
   canvas layout scan, idempotent source upload) before spawning `agy -p`.
2. Execute pure prompt-sync updates (`prompt_update_requested`) deterministically in ~1-2s
   when no unresolved `@` mentions or structural node changes require LLM reasoning,
   falling back cleanly to the LLM worker otherwise.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

import httpx

import jimeng_bridge
from store import SHOTS_DIR, get_platform_label, safe_prompt_update_prev_status

# Optional heavy media imports (PyAV + Pillow already live in .venv/runtime)
try:
    import av  # type: ignore
except Exception:
    av = None

try:
    from PIL import Image, ImageDraw  # type: ignore
except Exception:
    Image = None
    ImageDraw = None


_UNRESOLVED_AT_RE = re.compile(r"@(?:图片|视频|新图)\d*")
_NODE_TAG_RE = re.compile(r"\{\{node:([a-zA-Z0-9_-]+)\}\}")


def render_source_keyframes(source_path: Path, out_jpg: Path, cols: int = 3, rows: int = 3) -> Path | None:
    """Extract an evenly-spaced timestamped storyboard contact sheet from a video using PyAV+PIL."""
    if av is None or Image is None or not source_path.is_file():
        return None
    try:
        src_mtime = source_path.stat().st_mtime
        if out_jpg.is_file() and out_jpg.stat().st_size > 2048 and out_jpg.stat().st_mtime >= src_mtime:
            return out_jpg
    except OSError:
        return None

    target_count = max(1, cols * rows)
    frames_with_ts: list[tuple[float, Any]] = []
    try:
        with av.open(str(source_path)) as container:
            vstream = next((s for s in container.streams if s.type == "video"), None)
            if vstream is None:
                return None
            dur = float(vstream.duration * vstream.time_base) if vstream.duration and vstream.time_base else 0.0
            if dur <= 0.0 and container.duration:
                dur = float(container.duration) / float(av.time_base)
            # Sample timestamps evenly inside [0.05s, dur - 0.1s]
            all_decoded: list[tuple[float, Any]] = []
            approx_total = int(vstream.frames or 0)
            step = max(1, approx_total // max(target_count * 2, 18)) if approx_total > 0 else 1
            for idx, frame in enumerate(container.decode(video=0)):
                if step > 1 and (idx % step) != 0 and idx != 0:
                    continue
                t_sec = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else (idx / 24.0)
                all_decoded.append((t_sec, frame.to_image()))
            if not all_decoded:
                return None
            if len(all_decoded) <= target_count:
                frames_with_ts = all_decoded
            else:
                for k in range(target_count):
                    pick = round(k * (len(all_decoded) - 1) / max(1, target_count - 1))
                    frames_with_ts.append(all_decoded[pick])
    except Exception:
        return None

    if not frames_with_ts:
        return None

    cell_w = 420
    first_img = frames_with_ts[0][1]
    aspect = max(0.3, min(3.0, first_img.height / max(1, first_img.width)))
    cell_h = max(120, int(round(cell_w * aspect)))
    pad = 8
    actual_cols = min(cols, len(frames_with_ts))
    actual_rows = math.ceil(len(frames_with_ts) / max(1, actual_cols))
    canvas_w = pad + actual_cols * (cell_w + pad)
    canvas_h = pad + actual_rows * (cell_h + pad)
    sheet = Image.new("RGB", (canvas_w, canvas_h), (24, 24, 28))
    draw = ImageDraw.Draw(sheet) if ImageDraw is not None else None

    for idx, (t_sec, pil_img) in enumerate(frames_with_ts):
        r_idx = idx // actual_cols
        c_idx = idx % actual_cols
        x0 = pad + c_idx * (cell_w + pad)
        y0 = pad + r_idx * (cell_h + pad)
        thumb = pil_img.convert("RGB").resize((cell_w, cell_h), Image.Resampling.BILINEAR)
        sheet.paste(thumb, (x0, y0))
        if draw is not None:
            tag = f"#{idx + 1} {t_sec:.2f}s"
            draw.rectangle([x0 + 6, y0 + 6, x0 + 98, y0 + 28], fill=(0, 0, 0))
            draw.text((x0 + 11, y0 + 11), tag, fill=(255, 255, 255))

    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_jpg.with_name(f".{out_jpg.name}.tmp")
    sheet.save(tmp_out, format="JPEG", quality=86)
    tmp_out.replace(out_jpg)
    return out_jpg


def analyze_source_audio(source_path: Path, out_json: Path) -> dict[str, Any]:
    """Scan audio stream energy in 0.25s buckets using PyAV to flag active SFX/dialogue windows."""
    if not source_path.is_file():
        return {"hasAudioTrack": False}
    try:
        src_mtime = source_path.stat().st_mtime
        if out_json.is_file() and out_json.stat().st_mtime >= src_mtime:
            cached = json.loads(out_json.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return cached
    except Exception:
        pass

    summary: dict[str, Any] = {
        "hasAudioTrack": False,
        "durationSec": 0.0,
        "peakRmsDb": -96.0,
        "meanRmsDb": -96.0,
        "activeAudioWindows": [],
        "hint": "无音轨或未安装音频解码器",
    }
    if av is None:
        return summary

    try:
        with av.open(str(source_path)) as container:
            astream = next((s for s in container.streams if s.type == "audio"), None)
            if astream is None:
                summary["hint"] = "原片不含音频轨，可按纯SFX/静音规则写音频。"
                out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return summary

            summary["hasAudioTrack"] = True
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            bucket_samples = 4000  # 0.25s at 16kHz
            sum_sq = 0
            max_abs = 0
            count = 0
            bucket_idx = 0
            bucket_dbs: list[tuple[int, float, float]] = []
            total_sum_sq = 0
            total_samples = 0

            for frame in container.decode(audio=0):
                for out_frame in resampler.resample(frame):
                    pcm = memoryview(out_frame.planes[0]).cast("h")
                    for sample in pcm:
                        asmp = abs(int(sample))
                        sq = asmp * asmp
                        sum_sq += sq
                        if asmp > max_abs:
                            max_abs = asmp
                        count += 1
                        total_sum_sq += sq
                        total_samples += 1
                        if count >= bucket_samples:
                            rms = math.sqrt(sum_sq / count) / 32768.0
                            rms_db = 20.0 * math.log10(max(rms, 1e-5))
                            pk_db = 20.0 * math.log10(max(max_abs / 32768.0, 1e-5))
                            bucket_dbs.append((bucket_idx, round(rms_db, 1), round(pk_db, 1)))
                            bucket_idx += 1
                            sum_sq = 0
                            max_abs = 0
                            count = 0

            if count > 0:
                rms = math.sqrt(sum_sq / count) / 32768.0
                rms_db = 20.0 * math.log10(max(rms, 1e-5))
                pk_db = 20.0 * math.log10(max(max_abs / 32768.0, 1e-5))
                bucket_dbs.append((bucket_idx, round(rms_db, 1), round(pk_db, 1)))

            if total_samples > 0:
                summary["durationSec"] = round(total_samples / 16000.0, 2)
                mean_rms = math.sqrt(total_sum_sq / total_samples) / 32768.0
                summary["meanRmsDb"] = round(20.0 * math.log10(max(mean_rms, 1e-5)), 1)
                summary["peakRmsDb"] = max((pk for _, _, pk in bucket_dbs), default=-96.0)

            active_spans: list[tuple[float, float, float]] = []
            cur_start: float | None = None
            cur_end = 0.0
            cur_peak = -96.0
            for idx, rms_db, pk_db in bucket_dbs:
                t0 = idx * 0.25
                t1 = t0 + 0.25
                if pk_db >= -36.0 or rms_db >= -46.0:
                    if cur_start is None:
                        cur_start = t0
                        cur_peak = pk_db
                    else:
                        cur_peak = max(cur_peak, pk_db)
                    cur_end = t1
                elif cur_start is not None:
                    active_spans.append((cur_start, cur_end, cur_peak))
                    cur_start = None
            if cur_start is not None:
                active_spans.append((cur_start, cur_end, cur_peak))

            summary["activeAudioWindows"] = [
                f"{s:.2f}-{e:.2f}s (peak {p:.1f} dBFS)" for s, e, p in active_spans[:12]
            ]
            if not active_spans:
                summary["hint"] = "音轨能量全程低于 -42 dBFS（极静或无明显人声），仍请快速核对是否只有轻微环境音。"
            else:
                summary["hint"] = (
                    f"检测到 {len(active_spans)} 段有效声波窗口（峰值 {summary['peakRmsDb']} dBFS），"
                    "请用 read_file 听原片确认其中是动作 SFX 还是清晰对白。"
                )
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        summary["error"] = str(exc)
    return summary


def _run_dreamina_json(args: list[str], timeout: int = 45) -> dict[str, Any]:
    proc = jimeng_bridge._run_dreamina(args, timeout=timeout)
    payload = jimeng_bridge._extract_json(proc)
    if proc.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(jimeng_bridge._extract_error_msg(payload, proc.stderr.strip() or "dreamina CLI failed"))
    return payload


def _acquire_layout_lock(base_url: str, project_uuid: str, owner: str) -> str:
    if not base_url or not project_uuid:
        return ""
    try:
        with httpx.Client(base_url=base_url, timeout=18.0) as client:
            r = client.post(
                f"/api/agent/locks/canvas_layout_{project_uuid}/acquire",
                json={"owner": owner, "ttlSec": 35.0, "waitSec": 15.0},
            )
            if r.status_code == 200:
                return str(r.json().get("token") or "")
    except Exception:
        return ""
    return ""


def _release_layout_lock(base_url: str, project_uuid: str, token: str) -> None:
    if not base_url or not project_uuid or not token:
        return
    try:
        with httpx.Client(base_url=base_url, timeout=6.0) as client:
            client.post(f"/api/agent/locks/canvas_layout_{project_uuid}/release", json={"token": token})
    except Exception:
        pass


def inspect_dreamina_canvas_context(
    shot: dict[str, Any],
    *,
    base_url: str = "",
    preupload_source: bool = True,
) -> dict[str, Any]:
    """Read canvas node catalog, reuse role preset nodes, find collision-free Y, and optionally pre-upload source video."""
    result: dict[str, Any] = {}
    project_uuid = str(shot.get("projectUuid") or "").strip()
    shot_id = str(shot.get("id") or "").strip()
    if not project_uuid or str(shot.get("platform") or "").lower() != "jimeng":
        return result
    if not jimeng_bridge.get_auth_status().get("authenticated"):
        return result

    lock_tok = _acquire_layout_lock(base_url, project_uuid, f"{shot_id}_preprep") if preupload_source else ""
    try:
        nodes_summary: list[dict[str, Any]] = []
        offset = 0
        for _ in range(4):
            find_res = _run_dreamina_json(
                ["node", "find", "--project-id", project_uuid, "--limit", "50", "--offset", str(offset)],
                timeout=20,
            )
            data = find_res.get("data") or {}
            page_nodes = data.get("nodes") or []
            nodes_summary.extend(n for n in page_nodes if isinstance(n, dict))
            if not data.get("hasMore") or not page_nodes:
                break
            offset += len(page_nodes)

        existing_source_id = ""
        public_presets: dict[str, str] = {}
        char_labels = [str(c.get("label") or "").strip() for c in (shot.get("charRefs") or []) if isinstance(c, dict)]
        for item in nodes_summary:
            title = str(item.get("title") or "").strip()
            nid = str(item.get("nodeId") or "").strip()
            ntype = str(item.get("type") or "").strip()
            if not nid:
                continue
            if ntype == "video" and title in {f"{shot_id}-原片", f"{shot_id}_source"}:
                existing_source_id = nid
            if ntype == "image" and title.startswith("公共-"):
                role_name = title[len("公共-") :].strip()
                if role_name and role_name in char_labels and role_name not in public_presets:
                    public_presets[role_name] = nid

        # Compute bottom-most Y boundary across canvas nodes in batches of 25
        max_bottom = 0.0
        all_ids = [str(n.get("nodeId") or "").strip() for n in nodes_summary if n.get("nodeId")]
        for i in range(0, len(all_ids), 25):
            chunk = all_ids[i : i + 25]
            cmd = ["node", "show", "--project-id", project_uuid]
            for nid in chunk:
                cmd.extend(["--node-id", nid])
            try:
                show_res = _run_dreamina_json(cmd, timeout=25)
                for wrap in (show_res.get("data") or {}).get("nodes") or []:
                    node = wrap.get("node") if isinstance(wrap, dict) else None
                    lay = (node or {}).get("layout") if isinstance(node, dict) else None
                    if isinstance(lay, dict):
                        y_val = float(lay.get("y") or 0.0)
                        h_val = float(lay.get("height") or 320.0)
                        max_bottom = max(max_bottom, y_val + h_val)
            except Exception:
                continue

        suggested_y = float(int(math.ceil((max_bottom + 260.0) / 100.0) * 100)) if max_bottom > 0 else 0.0
        result["nodeCount"] = len(nodes_summary)
        result["suggestedRowY"] = suggested_y
        if public_presets:
            result["existingPublicRoleNodes"] = public_presets

        jm = shot.get("jimengMeta") if isinstance(shot.get("jimengMeta"), dict) else {}
        src_path = Path(str((shot.get("sourceVideo") or {}).get("filePath") or ""))
        if existing_source_id:
            result["preuploadedSourceNodeId"] = existing_source_id
        elif preupload_source and not jm.get("sourceNodeId") and src_path.is_file():
            up_res = _run_dreamina_json(
                [
                    "resource",
                    "upload",
                    "--project-id",
                    project_uuid,
                    "--file",
                    str(src_path),
                    "--name",
                    f"{shot_id}-原片",
                    "--import-kind",
                    "local_upload",
                ],
                timeout=120,
            )
            res_id = str((up_res.get("data") or {}).get("resourceId") or "").strip()
            if res_id:
                create_res = _run_dreamina_json(
                    [
                        "node",
                        "create",
                        "video",
                        "--project-id",
                        project_uuid,
                        "--title",
                        f"{shot_id}-原片",
                        "--import-kind",
                        "local_upload",
                        "--resource-id",
                        res_id,
                        "--x",
                        "1760",
                        "--y",
                        str(suggested_y),
                    ],
                    timeout=35,
                )
                created_nodes = (create_res.get("data") or {}).get("nodes") or []
                created_id = ""
                if created_nodes and isinstance(created_nodes[0], dict):
                    created_id = str(created_nodes[0].get("nodeId") or (created_nodes[0].get("node") or {}).get("nodeId") or "").strip()
                if not created_id:
                    created_id = str((create_res.get("data") or {}).get("nodeId") or "").strip()
                if created_id:
                    result["preuploadedSourceNodeId"] = created_id
                    result["preuploadedSourceResourceId"] = res_id
    except Exception as exc:
        result["canvasPrepWarning"] = str(exc)
    finally:
        _release_layout_lock(base_url, project_uuid, lock_tok)
    return result


def prepare_shot_artifacts(
    shot: dict[str, Any],
    *,
    base_url: str = "",
    preupload_source: bool = True,
) -> dict[str, Any]:
    """Generate local inspection artifacts + canvas coordinate/node context before invoking `agy -p`."""
    sid = str(shot.get("id") or "").strip()
    shot_dir = SHOTS_DIR / sid
    shot_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {}

    src_fp = str((shot.get("sourceVideo") or {}).get("filePath") or "").strip()
    if src_fp:
        src_path = Path(src_fp)
        kf_path = render_source_keyframes(src_path, shot_dir / "source_keyframes.jpg")
        if kf_path is not None:
            out["sourceKeyframesPath"] = str(kf_path)
        audio_info = analyze_source_audio(src_path, shot_dir / "source_audio_summary.json")
        out["sourceAudioSummary"] = audio_info

    task_type = str(shot.get("processingTaskType") or shot.get("status") or "")
    if str(shot.get("platform") or "").lower() == "jimeng" and task_type in {"queued", "recheck_requested"}:
        out.update(
            inspect_dreamina_canvas_context(
                shot,
                base_url=base_url,
                preupload_source=preupload_source and (task_type == "queued"),
            )
        )
    return out


def _sync_prompt_markdown(prompt_file: Path, shot_id: str, prompt_text: str, platform_label: str) -> Path:
    """Update the prompt body inside `outputs/{shot_id}_提示词.md` cleanly while preserving trailing notes."""
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    clean_body = prompt_text.strip()
    if prompt_file.is_file():
        existing = prompt_file.read_text(encoding="utf-8")
        pattern = re.compile(r"(```(?:text)?\s*\n)(.*?)(\n```)", re.DOTALL)
        if pattern.search(existing):
            updated = pattern.sub(lambda m: f"{m.group(1)}{clean_body}{m.group(3)}", existing, count=1)
            prompt_file.write_text(updated, encoding="utf-8")
            return prompt_file
    doc = (
        f"# {shot_id} 提示词（{platform_label} · Seedance 2.5）\n\n"
        f"## 提示词正文\n\n```text\n{clean_body}\n```\n"
    )
    prompt_file.write_text(doc, encoding="utf-8")
    return prompt_file


def try_fastpath_prompt_update(
    shot: dict[str, Any],
    *,
    base_url: str,
    workspace_root: Path,
) -> dict[str, Any]:
    """Deterministically sync a manual prompt edit or rollback to Dreamina Canvas without invoking an LLM."""
    sid = str(shot.get("id") or "").strip()
    claim_token = str(shot.get("claimToken") or "").strip()
    platform = str(shot.get("platform") or "libtv").strip().lower()
    if platform != "jimeng":
        return {"handled": False, "reason": "non_jimeng_platform"}

    jm = shot.get("jimengMeta") if isinstance(shot.get("jimengMeta"), dict) else {}
    project_uuid = str(shot.get("projectUuid") or jm.get("projectId") or "").strip()
    sample_node_id = str(jm.get("sampleNodeId") or jm.get("nodeId") or shot.get("libtvSampleNodeKey") or "").strip()
    prompt_text = str(shot.get("promptText") or shot.get("pendingPromptEdit") or "").strip()

    if not project_uuid or not sample_node_id.startswith("node_") or not prompt_text:
        return {"handled": False, "reason": "missing_project_node_or_prompt"}
    if _UNRESOLVED_AT_RE.search(prompt_text):
        return {"handled": False, "reason": "unresolved_at_mentions_require_agent"}

    try:
        show_res = _run_dreamina_json(
            ["node", "show", "--project-id", project_uuid, "--node-id", sample_node_id],
            timeout=25,
        )
        nodes = (show_res.get("data") or {}).get("nodes") or []
        node = (nodes[0] or {}).get("node") if nodes and isinstance(nodes[0], dict) else None
        if not isinstance(node, dict):
            return {"handled": False, "reason": "sample_node_not_found"}

        gen = node.get("generation") if isinstance(node.get("generation"), dict) else {}
        mode = str(gen.get("mode") or jm.get("mode") or "m2v").strip()
        model = str(gen.get("model") or jm.get("model") or "").strip()
        ratio = str(gen.get("ratio") or jm.get("ratio") or "").strip()
        resolution = str(gen.get("resolution") or jm.get("resolution") or "").strip()
        duration = gen.get("durationSeconds") or jm.get("duration")
        count = int(gen.get("outputCount") or 1)
        refs = [r for r in (gen.get("references") or []) if isinstance(r, dict) and r.get("id")]
        existing_node_ids = {str(r.get("id") or "") for r in refs if r.get("kind") == "node"}

        referenced_nodes = set(_NODE_TAG_RE.findall(prompt_text))
        if not referenced_nodes.issubset(existing_node_ids):
            return {"handled": False, "reason": "prompt_references_new_or_unlinked_node"}

        cmd = [
            "node",
            "edit",
            "video",
            "--project-id",
            project_uuid,
            "--node-id",
            sample_node_id,
            "--mode",
            mode,
            "--prompt",
            prompt_text,
        ]
        if model:
            cmd.extend(["--model", model])
        if ratio:
            cmd.extend(["--ratio", ratio])
        if resolution:
            cmd.extend(["--resolution", resolution])
        if duration:
            cmd.extend(["--duration", str(int(round(float(duration))))])
        if count > 0:
            cmd.extend(["--count", str(count)])
        for ref in refs:
            rkind = "res" if ref.get("kind") == "resource" else "node"
            cmd.extend(["--ref", f"{rkind}:{ref['id']}"])

        edit_res = _run_dreamina_json(cmd, timeout=35)
        draft_ver = (edit_res.get("data") or {}).get("draftVersion")

        quoted_max = jm.get("quotedMaxCredits")
        try:
            q_res = _run_dreamina_json(
                ["node", "quote", "--project-id", project_uuid, "--node-id", sample_node_id],
                timeout=25,
            )
            q_data = q_res.get("data") or {}
            if q_data.get("totalMaxCredits") is not None:
                quoted_max = q_data["totalMaxCredits"]
            if q_data.get("draftVersion") is not None:
                draft_ver = q_data["draftVersion"]
        except Exception:
            pass

        cur_prompt_file = str(shot.get("promptFilePath") or "").strip()
        md_path = Path(cur_prompt_file) if cur_prompt_file else (workspace_root / "outputs" / f"{sid}_提示词.md")
        _sync_prompt_markdown(md_path, sid, prompt_text, get_platform_label(platform))

        prev_status = str(shot.get("promptUpdatePrevStatus") or "").strip()
        target_status = safe_prompt_update_prev_status(shot, prev_status)
        if target_status not in {"ready_on_canvas", "draft_ready"}:
            target_status = "draft_ready" if (shot.get("draftVideo") or {}).get("mediaUrl") else "ready_on_canvas"

        meta_patch: dict[str, Any] = {"confirmationState": "quoted"}
        if draft_ver is not None:
            meta_patch["draftVersion"] = draft_ver
        if quoted_max is not None:
            meta_patch["quotedMaxCredits"] = quoted_max

        update_body: dict[str, Any] = {
            "claimToken": claim_token,
            "status": target_status,
            "promptText": prompt_text,
            "promptFilePath": str(md_path),
            "jimengMeta": meta_patch,
            "revisionDirection": "已通过快速通道将最新提示词同步至即梦画布节点与交付文档（未消耗积分，未触发生成）。",
            "revisionSummary": f"Python 快速通道已将提示词同步至即梦节点 {sample_node_id} 与 {md_path.name}",
            "agentSummary": f"已快速同步最新提示词到即梦画布节点 {sample_node_id}（当前预报价 {quoted_max or '-'} 积分），未自动生成；点击「▶ 按新修改重新生成」即可出片。",
        }
        with httpx.Client(base_url=base_url, timeout=15.0) as client:
            resp = client.post(f"/api/shots/{sid}/agent-update", json=update_body)
            if resp.status_code == 409:
                return {"handled": True, "conflict": True, "summary": "409 claimToken conflict"}
            resp.raise_for_status()
        return {"handled": True, "summary": f"fastpath synced {sample_node_id} (draftVersion={draft_ver})"}
    except Exception as exc:
        return {"handled": False, "reason": f"fastpath_fallback: {exc}"}
