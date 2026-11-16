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
import os
import re
import shlex
import shutil
import subprocess
import time
import warnings
import wave
from pathlib import Path
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        import audioop  # type: ignore
    except Exception:
        audioop = None  # type: ignore

import httpx

import jimeng_bridge
from store import DATA_ROOT, SHOTS_DIR, get_platform_label, resolve_cli_conversation_id, safe_prompt_update_prev_status

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
    cell_w = 420
    cell_h = 240
    frames_with_ts: list[tuple[float, Any]] = []
    try:
        with av.open(str(source_path)) as container:
            vstream = next((s for s in container.streams if s.type == "video"), None)
            if vstream is None:
                return None
            try:
                vstream.codec_context.thread_type = "AUTO"
            except Exception:
                pass
            if vstream.width and vstream.height:
                aspect = max(0.3, min(3.0, vstream.height / max(1, vstream.width)))
                cell_h = max(120, int(round(cell_w * aspect)))
            dur = float(vstream.duration * vstream.time_base) if vstream.duration and vstream.time_base else 0.0
            if dur <= 0.0 and container.duration:
                dur = float(container.duration) / float(av.time_base)
            # Multi-threaded decode + direct C-level libswscale downscale to thumbnail size
            all_decoded: list[tuple[float, Any]] = []
            approx_total = int(vstream.frames or 0)
            target_set = (
                {round(k * (approx_total - 1) / max(1, target_count - 1)) for k in range(target_count)}
                if approx_total > target_count
                else None
            )
            step = max(1, approx_total // max(target_count * 2, 18)) if (approx_total > 0 and target_set is None) else 1
            for idx, frame in enumerate(container.decode(video=0)):
                if target_set is not None:
                    if idx not in target_set:
                        continue
                elif step > 1 and (idx % step) != 0 and idx != 0:
                    continue
                t_sec = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else (idx / 24.0)
                if vstream.width and vstream.height:
                    pil_thumb = frame.reformat(width=cell_w, height=cell_h, format="rgb24").to_image()
                else:
                    pil_thumb = frame.to_image()
                all_decoded.append((t_sec, pil_thumb))
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

    first_img = frames_with_ts[0][1]
    if first_img.size != (cell_w, cell_h):
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
        thumb = pil_img if (pil_img.mode == "RGB" and pil_img.size == (cell_w, cell_h)) else pil_img.convert("RGB").resize((cell_w, cell_h), Image.Resampling.BILINEAR)
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


def analyze_source_audio(
    source_path: Path,
    out_json: Path,
    out_wav: Path | None = None,
) -> dict[str, Any]:
    """Scan audio energy in C-speed 0.25s buckets and optionally export lightweight 16kHz mono WAV."""
    if not source_path.is_file():
        return {"hasAudioTrack": False}
    try:
        src_mtime = source_path.stat().st_mtime
        if out_json.is_file() and out_json.stat().st_mtime >= src_mtime:
            cached = json.loads(out_json.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                wav_ready = (
                    out_wav is None
                    or not cached.get("hasAudioTrack")
                    or (out_wav.is_file() and out_wav.stat().st_mtime >= src_mtime)
                )
                if wav_ready:
                    return cached
    except Exception:
        pass

    summary: dict[str, Any] = {
        "hasAudioTrack": False,
        "durationSec": 0.0,
        "peakRmsDb": -96.0,
        "meanRmsDb": -96.0,
        "activeAudioWindows": [],
        "audioFilePath": "",
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
            bucket_bytes_target = 4000 * 2  # 0.25s at 16kHz 16-bit mono
            pcm_buf = bytearray()
            wav_chunks: list[bytes] | None = [] if out_wav is not None else None
            bucket_dbs: list[tuple[int, float, float]] = []
            bucket_idx = 0
            total_sum_sq = 0.0
            total_samples = 0

            def _score_bucket(chunk: bytes, idx: int) -> None:
                nonlocal total_sum_sq, total_samples
                ns = len(chunk) // 2
                if ns <= 0:
                    return
                if audioop is not None:
                    rms_val = audioop.rms(chunk, 2)
                    pk_val = audioop.max(chunk, 2)
                    total_sum_sq += float(rms_val * rms_val) * ns
                else:
                    pcm = memoryview(chunk).cast("h")
                    s_sq = sum(int(x) * int(x) for x in pcm)
                    pk_val = max((abs(int(x)) for x in pcm), default=0)
                    rms_val = math.sqrt(s_sq / ns)
                    total_sum_sq += s_sq
                total_samples += ns
                rms_db = 20.0 * math.log10(max(rms_val / 32768.0, 1e-5))
                pk_db = 20.0 * math.log10(max(pk_val / 32768.0, 1e-5))
                bucket_dbs.append((idx, round(rms_db, 1), round(pk_db, 1)))

            for frame in container.decode(audio=0):
                for out_frame in resampler.resample(frame):
                    raw_bytes = bytes(out_frame.planes[0])
                    if wav_chunks is not None:
                        wav_chunks.append(raw_bytes)
                    pcm_buf.extend(raw_bytes)
                    while len(pcm_buf) >= bucket_bytes_target:
                        _score_bucket(bytes(pcm_buf[:bucket_bytes_target]), bucket_idx)
                        del pcm_buf[:bucket_bytes_target]
                        bucket_idx += 1

            if len(pcm_buf) >= 2:
                if len(pcm_buf) % 2:
                    pcm_buf.pop()
                _score_bucket(bytes(pcm_buf), bucket_idx)

            if out_wav is not None and wav_chunks:
                out_wav.parent.mkdir(parents=True, exist_ok=True)
                tmp_wav = out_wav.with_name(f".{out_wav.name}.tmp")
                with wave.open(str(tmp_wav), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(b"".join(wav_chunks))
                tmp_wav.replace(out_wav)
                summary["audioFilePath"] = str(out_wav)

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
            wav_kb = round(out_wav.stat().st_size / 1024, 1) if (out_wav and out_wav.is_file()) else 0.0
            audio_note = f"可直接用 read_file 听极轻量的 {out_wav.name}（{wav_kb} KB，云端直听）" if wav_kb else "用 read_file 听原片"
            if not active_spans:
                summary["hint"] = "音轨能量全程低于 -42 dBFS（极静或无明显人声），按轻环境音处理即可；严禁跑本地 Whisper/ASR。"
            else:
                summary["hint"] = (
                    f"检测到 {len(active_spans)} 段有效声波窗口（峰值 {summary['peakRmsDb']} dBFS），"
                    f"请{audio_note}确认其中是动作 SFX 还是清晰对白；严禁跑本地 faster-whisper/PyTorch。"
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
        audio_info = analyze_source_audio(
            src_path,
            shot_dir / "source_audio_summary.json",
            out_wav=shot_dir / "source_audio.wav",
        )
        out["sourceAudioSummary"] = audio_info
        if audio_info.get("audioFilePath"):
            out["sourceAudioPath"] = audio_info["audioFilePath"]

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


def find_agy_cli_bin() -> str | None:
    home = Path.home()
    candidates = [
        os.environ.get("AGY_BIN", "").strip(),
        shutil.which("agy") or "",
        str(home / ".local" / "bin" / "agy"),
        str(home / ".gemini" / "antigravity" / "bin" / "agy"),
        "/Applications/Antigravity.app/Contents/Resources/app/bin/agy",
        str(home / "Applications" / "Antigravity.app/Contents/Resources/app/bin/agy"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return None


def build_shot_handoff_context(
    shot: dict[str, Any],
    *,
    workspace_root: Path,
    port: int = 8787,
    user_note: str = "",
) -> str:
    """Build a concise, rich markdown context brief for foreground chat takeover on a single shot."""
    sid = str(shot.get("id") or "").strip()
    plat = str(shot.get("platform") or "libtv").strip().lower()
    plat_label = get_platform_label(plat)
    jm = shot.get("jimengMeta") if isinstance(shot.get("jimengMeta"), dict) else {}
    src = shot.get("sourceVideo") or {}
    comp = shot.get("compImage") or {}
    draft = shot.get("draftVideo") or {}
    final = shot.get("finalVideo") or {}
    kf_path = SHOTS_DIR / sid / "source_keyframes.jpg"
    audio_json = SHOTS_DIR / sid / "source_audio_summary.json"
    conv_id = resolve_cli_conversation_id(shot)

    lines = [
        f"请帮我接手处理洗片控制台镜头 **{sid}**（当前状态：`{shot.get('status')}`，目标平台：**{plat_label}**）。",
    ]
    if user_note.strip():
        lines.extend(["", f"### 我当前最需要你解决的问题", user_note.strip()])

    lines.extend(
        [
            "",
            "### 基本上下文",
            f"- 所属场次：`{shot.get('scene') or '未分场'}`",
            f"- 工作区：`{workspace_root}` | 控制台地址：`http://127.0.0.1:{port}`",
            f"- 目标画布：`{shot.get('projectName') or '-'}` (`{shot.get('projectUuid') or '-'}`)",
            f"- 画布链接：{shot.get('canvasUrl') or '-'}",
            f"- 样片节点：`{jm.get('sampleNodeId') or jm.get('nodeId') or shot.get('libtvSampleNodeKey') or '-'}`",
        ]
    )
    if jm.get("compNodeId") or jm.get("zdepthNodeId") or jm.get("finalNodeId") or shot.get("libtvGroupKey"):
        lines.append(
            f"- 关联节点：comp=`{jm.get('compNodeId') or '-'}` | depth=`{jm.get('zdepthNodeId') or '-'}` | "
            f"final=`{jm.get('finalNodeId') or '-'}` | libtvGroup=`{shot.get('libtvGroupKey') or '-'}`"
        )
    if conv_id:
        lines.append(f"- 最近后台 agy 会话 ID：`{conv_id}`（可参考历史记录）")

    lines.extend(["", "### 本地素材与产物路径"])
    if src.get("filePath"):
        lines.append(f"- 原片视频：`{src['filePath']}`")
    if kf_path.is_file():
        lines.append(f"- 原片关键帧拼图：`{kf_path}`")
    if audio_json.is_file():
        lines.append(f"- 原片声波扫描：`{audio_json}`")
    if comp.get("filePath"):
        orig_note = f"（未涂白原图：`{comp['originalFilePath']}`）" if comp.get("originalFilePath") else ""
        lines.append(f"- 当前构图图：`{comp['filePath']}`{orig_note}")
    for idx, ch in enumerate(shot.get("charRefs") or [], start=2):
        lines.append(f"- 图片{idx} ({ch.get('label') or '角色'}): `{ch.get('filePath') or '-'}`")
    if draft.get("filePath"):
        lines.append(f"- 当前 480P 样片：`{draft['filePath']}`")
    if final.get("filePath"):
        lines.append(f"- 归档 1080P 正片：`{final['filePath']}`")
    prompt_file = str(shot.get("promptFilePath") or (workspace_root / "outputs" / f"{sid}_提示词.md"))
    lines.append(f"- 提示词主文件：`{prompt_file}`")

    if shot.get("errorReason") or shot.get("errorDetail"):
        lines.extend(
            [
                "",
                "### 最近报错诊断",
                f"- 原因：{shot.get('errorReason') or '-'}",
                f"- 建议：{shot.get('errorSolution') or '-'}",
                f"- 原始详情：`{str(shot.get('errorDetail') or '')[:600]}`",
            ]
        )

    pending_fbs = [f for f in (shot.get("feedbacks") or []) if isinstance(f, dict) and f.get("status") == "pending"]
    if pending_fbs:
        lines.extend(["", "### 当前待处理修改意见"])
        for fb in pending_fbs:
            lines.append(f"- [{fb.get('id')}] {fb.get('comment') or ''}")

    if shot.get("latestRevisionDirection"):
        lines.extend(["", f"### 最近一轮修改方向", str(shot.get("latestRevisionDirection"))])

    cur_prompt = str(shot.get("promptText") or shot.get("userPrompt") or "").strip()
    if cur_prompt:
        lines.extend(["", "### 当前提示词正文", "```text", cur_prompt, "```"])

    lines.extend(
        [
            "",
            "### 协同约定",
            f"- 修改并同步好画布后，请调用 `POST http://127.0.0.1:{port}/api/shots/{sid}/agent-update` 回写状态（如目前是人工前台修改，先 `POST /api/shots/{sid}/claim` 取 `claimToken` 再回写；**不要**运行任何队列常驻监听脚本）。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def launch_foreground_shot_chat(
    shot: dict[str, Any],
    *,
    mode: str = "auto",
    user_note: str = "",
    port: int = 8787,
    workspace_root: Path,
    agy_bin: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Launch an interactive macOS Terminal foreground `agy` session resuming or starting fresh on this shot."""
    sid = str(shot.get("id") or "").strip()
    eff_mode = (mode or "auto").strip().lower()
    if eff_mode not in {"auto", "resume", "new"}:
        eff_mode = "auto"

    conv_id = "" if eff_mode == "new" else resolve_cli_conversation_id(shot)
    if eff_mode == "resume" and not conv_id:
        raise RuntimeError(f"镜头 {sid} 暂无可续接的后台 agy 会话记录，请改用「新开独立前台对话」。")

    if str(shot.get("status") or "") == "processing" and conv_id:
        raise RuntimeError(f"后台桥接池正在处理 {sid}，请等待本轮完成或暂停后台桥接后再续接该会话。")

    resolved_agy = agy_bin or find_agy_bin()
    if not resolved_agy:
        raise FileNotFoundError("未找到本机 agy 命令行工具，请先安装 Antigravity CLI，或改用「复制上下文并唤起 Antigravity 桌面端」。")

    handoffs_dir = DATA_ROOT / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    safe_sid = re.sub(r"[^A-Za-z0-9._-]+", "_", sid).strip("_") or "shot"
    ctx_file = handoffs_dir / f"{safe_sid}_context.txt"
    ctx_text = build_shot_handoff_context(shot, workspace_root=workspace_root, port=port, user_note=user_note)
    ctx_file.write_text(ctx_text, encoding="utf-8")

    launcher = handoffs_dir / f"{safe_sid}_chat.command"
    plat_label = get_platform_label(shot.get("platform"))
    q_ws = shlex.quote(str(workspace_root))
    q_agy = shlex.quote(str(resolved_agy))
    q_ctx = shlex.quote(str(ctx_file))

    script_lines = [
        "#!/bin/zsh",
        'export PATH="$HOME/.local/bin:$HOME/.libtv:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
        f"cd {q_ws} || exit 1",
        "clear",
        'echo "=================================================="',
        f'echo {shlex.quote(f"  洗片控制台 · 前台对话接手 [{sid}]")}',
        f'echo {shlex.quote(f"  平台: {plat_label} | 状态: {shot.get("status") or "-"}")}',
        'echo "  退出前台对话请按 Ctrl+D 或输入 /exit"',
        'echo "=================================================="',
        'echo ""',
    ]
    if conv_id:
        q_conv = shlex.quote(conv_id)
        script_lines.append(f'echo {shlex.quote(f"正在恢复该镜最近后台会话: {conv_id}")}')
        if user_note.strip():
            script_lines.append(f'echo {shlex.quote(f"提示: 本次补充关注点已保存至 {ctx_file}")}')
        script_lines.append(f"exec {q_agy} --conversation {q_conv}")
        launch_mode = "resume"
    else:
        script_lines.extend(
            [
                f'echo {shlex.quote(f"正在新开前台对话并注入 {sid} 完整上下文...")}',
                f'PROMPT_CONTENT="$(cat {q_ctx})"',
                f'exec {q_agy} -i "$PROMPT_CONTENT"',
            ]
        )
        launch_mode = "new"

    launcher.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    os.chmod(launcher, 0o755)

    if not dry_run:
        proc = subprocess.run(
            ["/usr/bin/open", "-a", "Terminal", str(launcher)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "无法打开 Terminal 前台窗口")

    return {
        "ok": True,
        "shotId": sid,
        "mode": launch_mode,
        "conversationId": conv_id,
        "contextPath": str(ctx_file),
        "launcherPath": str(launcher),
    }


def launch_foreground_app_or_terminal(
    *,
    target: str = "antigravity",
    workspace_root: Path,
    agy_bin: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Open Antigravity Desktop GUI or a clean interactive Terminal `agy` window."""
    clean_target = (target or "antigravity").strip().lower()
    if clean_target == "terminal_agy":
        clean_target = "terminal"
    if clean_target not in {"terminal", "antigravity"}:
        raise ValueError(f"不支持的启动目标: {target}")

    if clean_target == "terminal":
        resolved_agy = agy_bin or find_agy_bin()
        if not resolved_agy:
            raise FileNotFoundError("未找到 agy 命令行工具")
        handoffs_dir = DATA_ROOT / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        launcher = handoffs_dir / "open_fresh_chat.command"
        launcher.write_text(
            "\n".join(
                [
                    "#!/bin/zsh",
                    'export PATH="$HOME/.local/bin:$HOME/.libtv:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
                    f"cd {shlex.quote(str(workspace_root))} || exit 1",
                    "clear",
                    'echo "=== Antigravity CLI 交互窗口（按 Ctrl+D 退出）==="',
                    f"exec {shlex.quote(str(resolved_agy))}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(launcher, 0o755)
        if not dry_run:
            proc = subprocess.run(
                ["/usr/bin/open", "-a", "Terminal", str(launcher)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "无法打开 Terminal 前台窗口")
        return {"ok": True, "target": "terminal", "launcherPath": str(launcher)}

    if not dry_run:
        proc = subprocess.run(["/usr/bin/open", "-a", "Antigravity"], capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "未找到 Antigravity 桌面应用")
    return {"ok": True, "target": "antigravity"}
