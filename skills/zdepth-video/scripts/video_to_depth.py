#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch",
#   "torchvision",
#   "transformers",
#   "pillow",
#   "numpy",
#   "imageio-ffmpeg",
# ]
# ///
"""
将任意输入视频洗成 720P 无闪烁黑白灰阶景深视频（Z-Depth Map，近亮远暗），保留原帧率、时长与原声音轨。
高性能流式版（方案 A）：FFmpeg 前置缩放至目标 720P 并通过内存管道直通推理与单通道编码，零临时图片落盘；
支持 Apple Silicon (MPS) / NVIDIA GPU (CUDA) / CPU，自动适配 FFmpeg 4.x–9.x，内置国内 HF 镜像自动回退。
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import time

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForDepthEstimation


def resolve_ffmpeg_exe() -> str:
    """定位可用的 FFmpeg 可执行文件（优先使用内置 imageio-ffmpeg）。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError("未找到可用的 FFmpeg，请安装 imageio-ffmpeg 或系统 ffmpeg。")


def check_ffmpeg_capabilities(ffmpeg_exe: str) -> tuple[bool, bool]:
    """检测当前 FFmpeg 是否支持 -fps_mode (5.1+) 与 cas 锐化滤镜 (4.3+)。"""
    res_opt = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-h", "long"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    opt_text = (res_opt.stdout or "") + (res_opt.stderr or "")
    has_fps_mode = "-fps_mode" in opt_text

    res_flt = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-filters"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    flt_text = (res_flt.stdout or "") + (res_flt.stderr or "")
    has_cas = bool(re.search(r"\bcas\b", flt_text))
    return has_fps_mode, has_cas


def gaussian_smooth_1d(arr: np.ndarray, sigma: float = 18.0) -> np.ndarray:
    """一维高斯滤波，用于消除全片分位数跳变导致的呼吸闪烁。"""
    if len(arr) <= 3 or sigma <= 0:
        return arr.copy()
    radius = int(min(len(arr) - 1, max(3, round(sigma * 3))))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(arr, (radius, radius), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def probe_video(ffmpeg_exe: str, input_path: str) -> tuple[float, bool, float]:
    """探测输入视频总时长、是否存在音轨以及容器标称帧率。"""
    res = subprocess.run(
        [ffmpeg_exe, "-i", input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    duration = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
    if m:
        h, m_min, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = h * 3600 + m_min * 60 + s

    probed_fps = 24.0
    m_fps = re.search(r"Video:.*?(\d+(?:\.\d+)?)\s*fps", res.stderr)
    if m_fps:
        val = float(m_fps.group(1))
        if val > 0:
            probed_fps = val

    has_audio = "Audio:" in res.stderr
    return duration, has_audio, probed_fps


def probe_first_frame_size(ffmpeg_exe: str, input_path: str) -> tuple[int, int]:
    """内存中解码首帧以获取经旋转元数据校正后的真实画面宽高。"""
    res = subprocess.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-vframes",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if res.returncode != 0 or not res.stdout:
        err = res.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"FFmpeg 探测视频首帧尺寸失败:\n{err}")
    with Image.open(io.BytesIO(res.stdout)) as img:
        return img.size


def compute_target_720p_size(
    orig_w: int,
    orig_h: int,
    explicit_res: str | None = None,
    exact_aspect: bool = False,
) -> tuple[int, int]:
    """根据原片比例或显式参数计算标准 720P 输出分辨率。"""
    if explicit_res:
        m = re.match(r"^(\d+)x(\d+)$", explicit_res.strip().lower())
        if not m:
            raise ValueError(f"无效的分辨率格式: {explicit_res}，应类似 1680x720")
        return int(m.group(1)), int(m.group(2))

    aspect = orig_w / float(orig_h)
    if exact_aspect:
        if orig_w >= orig_h:
            th = 720
            tw = int(round(720 * aspect / 2.0)) * 2
        else:
            tw = 720
            th = int(round(720 / aspect / 2.0)) * 2
        return tw, th

    # 标准 720P 档位自动匹配（适配 Seedance / VACE / ComfyUI 常用画幅）
    if aspect >= 2.05:
        return 1680, 720       # 21:9 电影宽银幕 720P
    elif 1.55 <= aspect < 2.05:
        return 1280, 720       # 16:9 横屏 720P
    elif aspect <= 0.65:
        return 720, 1280       # 9:16 竖屏 720P
    elif orig_w >= orig_h:
        th = 720
        tw = max(720, int(round(720 * aspect / 16.0)) * 16)
        return tw, th
    else:
        tw = 720
        th = max(720, int(round(720 / aspect / 16.0)) * 16)
        return tw, th


def select_device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def resolve_batch_size(device: torch.device, user_batch_size: int | None) -> int:
    """根据计算设备选择兼顾吞吐与内存安全的默认批大小（MPS 过大 batch 易触发统一内存换页降速）。"""
    if user_batch_size is not None:
        return max(1, user_batch_size)
    return 8 if device.type == "cuda" else 4


def load_depth_model(model_id: str, device: torch.device):
    print(f"[1/3] 正在加载深度估计模型: {model_id} (device={device}) ...")
    # 1) 优先从本地 HuggingFace 缓存离线秒加载
    try:
        processor = AutoImageProcessor.from_pretrained(model_id, local_files_only=True)
        model = AutoModelForDepthEstimation.from_pretrained(model_id, local_files_only=True).to(device).eval()
        return processor, model
    except Exception:
        pass

    # 2) 本地尚未缓存时在线下载，若 HuggingFace 直连受限则自动切换国内镜像
    try:
        processor = AutoImageProcessor.from_pretrained(model_id)
        model = AutoModelForDepthEstimation.from_pretrained(model_id).to(device).eval()
        return processor, model
    except Exception as e:
        print(f"直连 HuggingFace 失败 ({e})，自动切换 hf-mirror.com 镜像下载...")
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        processor = AutoImageProcessor.from_pretrained(model_id)
        model = AutoModelForDepthEstimation.from_pretrained(model_id).to(device).eval()
        return processor, model


def read_exact(stream, num_bytes: int) -> bytearray:
    """从管道流中读取最多 num_bytes 字节到可写 bytearray（供零拷贝转成可写 NumPy 数组）。"""
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = stream.read(num_bytes - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return buf


def terminate_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def stream_decode_and_infer(
    ffmpeg_exe: str,
    input_video: str,
    target_w: int,
    target_h: int,
    has_fps_mode: bool,
    batch_size: int,
    device: torch.device,
    processor,
    model,
) -> tuple[list[np.ndarray], np.ndarray, np.ndarray, int]:
    """
    通过 FFmpeg 管道将视频前置缩放至目标 720P 并逐批流式推理单目深度图。
    返回: (float16 深度分块列表, 每帧 1.0% 分位数, 每帧 99.6% 分位数, 实际总帧数)
    """
    vsync_flags = ["-fps_mode", "passthrough"] if has_fps_mode else ["-vsync", "0"]
    decode_vf = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={target_w}:{target_h},format=rgb24"
    )
    dec_cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_video,
        "-map",
        "0:v:0",
        *vsync_flags,
        "-vf",
        decode_vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]

    frame_bytes = target_w * target_h * 3
    read_chunk_bytes = frame_bytes * batch_size

    depth_chunks: list[np.ndarray] = []
    p_low_list: list[float] = []
    p_high_list: list[float] = []
    total_frames = 0

    print(f"[2/3] 正在流式 720P 解码并逐批推理深度图 (batch_size={batch_size})...")
    t0 = time.time()
    dec_proc = subprocess.Popen(
        dec_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=min(frame_bytes * max(2, batch_size), 32 * 1024 * 1024),
    )

    try:
        assert dec_proc.stdout is not None and dec_proc.stderr is not None
        while True:
            buf = read_exact(dec_proc.stdout, read_chunk_bytes)
            if not buf:
                break
            if len(buf) % frame_bytes != 0:
                raise RuntimeError(
                    f"FFmpeg 解码流帧字节未对齐: 收到 {len(buf)} 字节，非单帧 {frame_bytes} 字节整数倍"
                )

            cur_bs = len(buf) // frame_bytes
            batch_rgb = np.frombuffer(buf, dtype=np.uint8).reshape(cur_bs, target_h, target_w, 3)

            inputs = processor(images=list(batch_rgb), return_tensors="pt")
            inputs = {k: v.to(device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = model(**inputs)
                pred = outputs.predicted_depth
                pred_resized = F.interpolate(
                    pred.unsqueeze(1),
                    size=(target_h, target_w),
                    mode="bicubic",
                    align_corners=False,
                ).squeeze(1)

            batch_depths = pred_resized.cpu().numpy().astype(np.float32)
            # 在释放 float32 批次前即时计算双分位数，兼顾浮点精度与零二次遍历
            qs = np.percentile(batch_depths.reshape(cur_bs, -1), [1.0, 99.6], axis=1)
            p_low_list.extend(qs[0].tolist())
            p_high_list.extend(qs[1].tolist())
            depth_chunks.append(batch_depths.astype(np.float16))

            prev_bucket = total_frames // 40
            total_frames += cur_bs
            if (total_frames // 40) > prev_bucket:
                elapsed = time.time() - t0
                print(f"      已推理 {total_frames} 帧 ({elapsed:.1f}s)")

        dec_proc.stdout.close()
        stderr_data = dec_proc.stderr.read().decode("utf-8", errors="replace")
        dec_rc = dec_proc.wait()
        if dec_rc != 0:
            raise RuntimeError(f"FFmpeg 流式解码失败 (code={dec_rc}):\n{stderr_data}")
    except Exception:
        terminate_process(dec_proc)
        raise

    if total_frames == 0:
        raise RuntimeError("未能从输入视频中读取到任何视频帧！")

    elapsed = time.time() - t0
    print(f"      推理完成: 共 {total_frames} 帧，耗时 {elapsed:.1f}s")
    return (
        depth_chunks,
        np.asarray(p_low_list, dtype=np.float64),
        np.asarray(p_high_list, dtype=np.float64),
        total_frames,
    )


def stream_stabilize_and_encode(
    ffmpeg_exe: str,
    input_video: str,
    output_video: str,
    depth_chunks: list[np.ndarray],
    p_low: np.ndarray,
    p_high: np.ndarray,
    total_frames: int,
    target_w: int,
    target_h: int,
    fps: float,
    has_cas: bool,
    audio_exists: bool,
    mute: bool,
    gamma: float,
    invert: bool,
    preset: str,
) -> None:
    """时序平滑归一化后，通过单通道灰度管道直接流式编码为 720P MP4。"""
    smooth_low = gaussian_smooth_1d(p_low, sigma=18.0)
    smooth_high = gaussian_smooth_1d(p_high, sigma=18.0)

    os.makedirs(os.path.dirname(output_video) or ".", exist_ok=True)
    sharpen_flt = "cas=0.25" if has_cas else "unsharp=3:3:0.25:3:3:0.0"
    vf_chain = f"{sharpen_flt},format=yuv420p"

    enc_cmd = [
        ffmpeg_exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s",
        f"{target_w}x{target_h}",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        "pipe:0",
    ]
    if audio_exists and not mute:
        enc_cmd += ["-i", input_video, "-map", "0:v:0", "-map", "1:a:0?", "-c:a", "copy", "-shortest"]
    enc_cmd += [
        "-vf",
        vf_chain,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        "15",
        "-movflags",
        "+faststart",
        output_video,
    ]

    print(f"[3/3] 正在时序稳定并流式编码 720P ({target_w}x{target_h}, preset={preset}) MP4...")
    t0 = time.time()
    enc_proc = subprocess.Popen(
        enc_cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=target_w * target_h * 4,
    )

    try:
        assert enc_proc.stdin is not None and enc_proc.stderr is not None
        prev_norm = None
        frame_idx = 0
        for chunk in depth_chunks:
            for depth_f16 in chunk:
                lo = float(smooth_low[frame_idx])
                hi = float(max(smooth_high[frame_idx], lo + 1e-5))
                norm = np.clip((depth_f16.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0)

                if gamma != 1.0 and gamma > 0:
                    norm = np.power(norm, gamma)

                if invert:
                    norm = 1.0 - norm

                # 自适应静态背景微降噪（运动前景零拖影）
                if prev_norm is not None:
                    diff = np.abs(norm - prev_norm)
                    alpha = np.clip((diff - 0.012) / (0.040 - 0.012), 0.0, 1.0)
                    blend_curr = 0.70 + 0.30 * alpha
                    norm = blend_curr * norm + (1.0 - blend_curr) * prev_norm
                prev_norm = norm

                gray_u8 = np.round(norm * 255.0).astype(np.uint8)
                try:
                    enc_proc.stdin.write(gray_u8.tobytes())
                except BrokenPipeError:
                    break
                frame_idx += 1

        try:
            enc_proc.stdin.close()
        except BrokenPipeError:
            pass
        stderr_data = enc_proc.stderr.read().decode("utf-8", errors="replace")
        enc_rc = enc_proc.wait()
        if enc_rc != 0:
            raise RuntimeError(f"FFmpeg 流式编码失败 (code={enc_rc}):\n{stderr_data}")
        if frame_idx != total_frames:
            raise RuntimeError(f"编码写入帧数不完整: {frame_idx}/{total_frames}")
    except Exception:
        terminate_process(enc_proc)
        raise

    print(f"✅ 720P 景深视频已生成: {output_video} ({target_w}x{target_h}, 编码耗时 {time.time() - t0:.1f}s)")


def parse_args():
    parser = argparse.ArgumentParser(description="将视频转换为 720P 无闪烁黑白灰阶景深视频 (Z-Depth)")
    parser.add_argument("input_video", help="输入视频路径")
    parser.add_argument("output_video", help="输出 MP4 路径")
    parser.add_argument("--resolution", default=None, help="显式指定输出分辨率，如 1680x720 或 1280x720（默认自动按画幅输出 720P）")
    parser.add_argument("--exact-aspect", action="store_true", help="严格按原片精确宽高比缩放到短边 720（不做标准 21:9/16:9 微裁切）")
    parser.add_argument("--gamma", type=float, default=1.0, help="灰阶 Gamma 调整（<1.0 提亮中后景，默认 1.0 保持标准线性视差）")
    parser.add_argument("--invert", action="store_true", help="反转黑白（变为近黑远白，默认关闭即近白远黑）")
    parser.add_argument("--mute", action="store_true", help="不保留原视频音轨（默认无损保留原声音轨）")
    parser.add_argument("--batch-size", type=int, default=None, help="批处理大小（默认 CUDA=8，MPS/CPU=4）")
    parser.add_argument("--preset", default="medium", help="H.264 编码预设（默认 medium 兼顾速度与体积，可选 fast/slow）")
    parser.add_argument("--model", default="depth-anything/Depth-Anything-V2-Base-hf", help="HuggingFace 深度模型 ID")
    return parser.parse_args()


def main():
    args = parse_args()
    input_video = os.path.abspath(args.input_video)
    output_video = os.path.abspath(args.output_video)

    if not os.path.isfile(input_video):
        raise FileNotFoundError(f"找不到输入视频: {input_video}")

    ffmpeg_exe = resolve_ffmpeg_exe()
    has_fps_mode, has_cas = check_ffmpeg_capabilities(ffmpeg_exe)
    duration, audio_exists, probed_fps = probe_video(ffmpeg_exe, input_video)
    orig_w, orig_h = probe_first_frame_size(ffmpeg_exe, input_video)
    target_w, target_h = compute_target_720p_size(orig_w, orig_h, args.resolution, args.exact_aspect)

    device = select_device()
    batch_size = resolve_batch_size(device, args.batch_size)
    print(f"      原片: {orig_w}x{orig_h} ({duration:.2f}s) -> 目标 720P 规格: {target_w}x{target_h}")

    processor, model = load_depth_model(args.model, device)
    depth_chunks, p_low, p_high, total_frames = stream_decode_and_infer(
        ffmpeg_exe=ffmpeg_exe,
        input_video=input_video,
        target_w=target_w,
        target_h=target_h,
        has_fps_mode=has_fps_mode,
        batch_size=batch_size,
        device=device,
        processor=processor,
        model=model,
    )

    fps = total_frames / duration if duration > 0 else probed_fps
    print(f"      实际帧数: {total_frames} 帧, 计算帧率: {fps:.4f} fps")
    stream_stabilize_and_encode(
        ffmpeg_exe=ffmpeg_exe,
        input_video=input_video,
        output_video=output_video,
        depth_chunks=depth_chunks,
        p_low=p_low,
        p_high=p_high,
        total_frames=total_frames,
        target_w=target_w,
        target_h=target_h,
        fps=fps,
        has_cas=has_cas,
        audio_exists=audio_exists,
        mute=args.mute,
        gamma=args.gamma,
        invert=args.invert,
        preset=args.preset,
    )


if __name__ == "__main__":
    main()
