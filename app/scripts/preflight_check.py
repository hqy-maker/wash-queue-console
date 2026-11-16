#!/usr/bin/env python3
"""
Seedance / LibTV 批量洗视频上传前本地预检脚本。

检查项：
1. 图片：格式（jpeg/png/webp/bmp/tiff/gif/heic）、宽高（300–6000px）、宽高比（0.4–2.5）、体积（<30MB）
2. 视频：文件存在性、时长（是否在 4–30s 范围内）以及建议的整数秒 duration
   （优先用 ffprobe，其次尝试 PyAV / imageio_ffmpeg）

用法：
  python3 preflight_check.py <文件或目录...>
  python3 preflight_check.py --json <文件或目录...>
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MIN_DIM = 300
MAX_DIM = 6000
MIN_RATIO = 0.4
MAX_RATIO = 2.5


def _parse_png(data: bytes):
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None


def _parse_gif(data: bytes):
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    return None


def _parse_bmp(data: bytes):
    if len(data) >= 26 and data[:2] == b"BM":
        w, h = struct.unpack("<ii", data[18:26])
        return abs(w), abs(h)
    return None


def _parse_jpeg(f):
    f.seek(0)
    if f.read(2) != b"\xff\xd8":
        return None
    while True:
        marker = f.read(2)
        while marker and marker[0] == 0xFF and marker[1] == 0xFF:
            marker = b"\xff" + f.read(1)
        if len(marker) < 2 or marker[0] != 0xFF:
            return None
        code = marker[1]
        if code in (0xD8, 0xD9) or (0xD0 <= code <= 0xD7):
            continue
        seg_len_bytes = f.read(2)
        if len(seg_len_bytes) < 2:
            return None
        seg_len = struct.unpack(">H", seg_len_bytes)[0]
        if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            sof = f.read(5)
            if len(sof) < 5:
                return None
            h, w = struct.unpack(">HH", sof[1:5])
            return w, h
        f.seek(seg_len - 2, os.SEEK_CUR)


def _parse_webp(data: bytes):
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return w, h
    if chunk == b"VP8L" and len(data) >= 25:
        bits = struct.unpack("<I", data[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    if chunk == b"VP8X" and len(data) >= 30:
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    return None


def get_image_dimensions(path: Path):
    ext = path.suffix.lower()
    with path.open("rb") as f:
        head = f.read(64)
        if ext == ".png":
            dims = _parse_png(head)
        elif ext == ".gif":
            dims = _parse_gif(head)
        elif ext == ".bmp":
            dims = _parse_bmp(head)
        elif ext in (".jpg", ".jpeg"):
            dims = _parse_jpeg(f)
        elif ext == ".webp":
            dims = _parse_webp(head)
        else:
            dims = None
    if dims:
        return dims
    # Fallback to Pillow or macOS sips if available
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            return im.size
    except Exception:
        pass
    if shutil.which("sips"):
        try:
            out = subprocess.check_output(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            w = h = None
            for line in out.splitlines():
                if "pixelWidth:" in line:
                    w = int(line.split(":")[-1].strip())
                elif "pixelHeight:" in line:
                    h = int(line.split(":")[-1].strip())
            if w and h:
                return w, h
        except Exception:
            pass
    return None


def _parse_mp4_duration(path: Path):
    """Parse MP4/MOV moov/mvhd duration in pure Python as a zero-dependency fallback."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as f:
            pos = 0
            while pos + 8 <= file_size:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size, box_type = struct.unpack(">I4s", hdr)
                header_size = 8
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                    header_size = 16
                elif size == 0:
                    size = file_size - pos
                if size < header_size:
                    break
                if box_type == b"moov":
                    moov_end = pos + size
                    sub_pos = pos + header_size
                    while sub_pos + 8 <= moov_end:
                        f.seek(sub_pos)
                        shdr = f.read(8)
                        if len(shdr) < 8:
                            break
                        ssize, sbox = struct.unpack(">I4s", shdr)
                        sheader = 8
                        if ssize == 1:
                            sext = f.read(8)
                            if len(sext) < 8:
                                break
                            ssize = struct.unpack(">Q", sext)[0]
                            sheader = 16
                        elif ssize == 0:
                            ssize = moov_end - sub_pos
                        if ssize < sheader:
                            break
                        if sbox == b"mvhd":
                            f.seek(sub_pos + sheader)
                            ver_flags = f.read(4)
                            if len(ver_flags) < 4:
                                return None
                            version = ver_flags[0]
                            if version == 0:
                                data = f.read(16)
                                if len(data) < 16:
                                    return None
                                _, _, timescale, duration = struct.unpack(">IIII", data)
                            elif version == 1:
                                data = f.read(28)
                                if len(data) < 28:
                                    return None
                                _, _, timescale, duration = struct.unpack(">QQIQ", data)
                            else:
                                return None
                            if timescale > 0:
                                return round(duration / timescale, 3)
                            return None
                        sub_pos += ssize
                    return None
                pos += size
    except Exception:
        return None
    return None


def get_video_info(path: Path):
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.check_output(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,width,height",
                    "-of",
                    "json",
                    str(path),
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            info = json.loads(out)
            dur = float(info.get("format", {}).get("duration", 0))
            streams = info.get("streams", [])
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
            return {
                "durationSec": round(dur, 3),
                "width": vstream.get("width"),
                "height": vstream.get("height"),
                "hasAudio": has_audio,
            }
        except Exception:
            pass
    try:
        import av  # type: ignore

        with av.open(str(path)) as container:
            dur = (container.duration or 0) / av.time_base if container.duration else 0.0
            vstream = next((s for s in container.streams if s.type == "video"), None)
            has_audio = any(s.type == "audio" for s in container.streams)
            return {
                "durationSec": round(float(dur), 3),
                "width": vstream.width if vstream else None,
                "height": vstream.height if vstream else None,
                "hasAudio": has_audio,
            }
    except Exception:
        pass
    if path.suffix.lower() in (".mp4", ".mov", ".m4v"):
        dur = _parse_mp4_duration(path)
        if dur is not None:
            return {
                "durationSec": dur,
                "width": None,
                "height": None,
                "hasAudio": None,
            }
    return None


def check_image(path: Path):
    size = path.stat().st_size
    issues = []
    if size >= MAX_IMAGE_BYTES:
        issues.append(f"size {size / (1024 * 1024):.2f}MB >= 30MB")
    dims = get_image_dimensions(path)
    w = h = ratio = None
    if dims:
        w, h = dims
        ratio = round(w / h, 3) if h else None
        if not (MIN_DIM <= w <= MAX_DIM and MIN_DIM <= h <= MAX_DIM):
            issues.append(f"dimensions {w}x{h} outside {MIN_DIM}-{MAX_DIM}px")
        if ratio is not None and not (MIN_RATIO <= ratio <= MAX_RATIO):
            issues.append(f"aspect ratio {ratio} outside {MIN_RATIO}-{MAX_RATIO}")
    else:
        issues.append("could not inspect dimensions")
    return {
        "path": str(path),
        "kind": "image",
        "bytes": size,
        "width": w,
        "height": h,
        "aspectRatio": ratio,
        "ok": len(issues) == 0,
        "issues": issues,
    }


def check_video(path: Path):
    size = path.stat().st_size
    info = get_video_info(path)
    issues = []
    suggested_duration = None
    if info and info.get("durationSec"):
        dur = info["durationSec"]
        rounded = int(round(dur))
        suggested_duration = max(4, min(30, rounded))
        if dur < 3.5 or dur > 30.4:
            issues.append(f"duration {dur}s outside Seedance 2.5 range (4-30s); clamped={suggested_duration}s")
    else:
        issues.append("could not inspect video duration (ffprobe/av not found)")
    return {
        "path": str(path),
        "kind": "video",
        "bytes": size,
        **(info or {}),
        "suggestedDuration": suggested_duration,
        "ok": len(issues) == 0,
        "issues": issues,
    }


def collect_files(targets):
    files = []
    for t in targets:
        p = Path(t).expanduser().resolve()
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for item in sorted(p.rglob("*")):
                if item.is_file() and not item.name.startswith("."):
                    files.append(item)
    return files


def main():
    parser = argparse.ArgumentParser(description="Preflight check images and videos for Seedance / LibTV.")
    parser.add_argument("targets", nargs="+", help="Files or directories to inspect")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    results = []
    for f in collect_files(args.targets):
        ext = f.suffix.lower()
        if ext in IMAGE_EXTS:
            results.append(check_image(f))
        elif ext in VIDEO_EXTS:
            results.append(check_video(f))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            status = "OK" if r["ok"] else "WARN"
            if r["kind"] == "image":
                detail = f"{r['width']}x{r['height']} ratio={r['aspectRatio']} {r['bytes']/1024:.0f}KB"
            else:
                detail = f"dur={r.get('durationSec')}s -> {r.get('suggestedDuration')}s audio={r.get('hasAudio')}"
            issue_txt = f" | issues: {'; '.join(r['issues'])}" if r["issues"] else ""
            print(f"[{status}] {r['kind']} | {detail} | {r['path']}{issue_txt}")

    has_fail = any(not r["ok"] for r in results)
    sys.exit(1 if has_fail else 0)


if __name__ == "__main__":
    main()
