#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
#   "numpy",
# ]
# ///
"""构图/影调参考图最小必要纯白 (#FFFFFF) 局部遮罩工具。

用法：
  1) 生成 0..1000 归一化坐标网格辅助图（可选局部裁切放大）：
     uv run whiteout_mask.py grid -i comp.jpg -o grid.jpg [--crop 400,50,800,700]

  2) 按坐标清单将指定局部涂白为 #FFFFFF，其余像素 100% 原样保留：
     uv run whiteout_mask.py apply -i comp.jpg -o comp_masked.png --spec mask.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def norm_to_px(x: float, y: float, width: int, height: int, space: str) -> tuple[int, int]:
    """将 0..1000 归一化坐标或像素坐标转换为图像整数像素坐标。"""
    if space == "px":
        return int(round(x)), int(round(y))
    return int(round(x * width / 1000.0)), int(round(y * height / 1000.0))


def parse_crop(crop_str: str | None, width: int, height: int) -> tuple[int, int, int, int]:
    """解析可选 0..1000 裁切框，返回像素 (x1, y1, x2, y2)。"""
    if not crop_str:
        return 0, 0, width, height
    parts = [float(p.strip()) for p in crop_str.split(",")]
    if len(parts) != 4:
        raise ValueError("--crop 格式应为 x1,y1,x2,y2（0..1000 归一化坐标）")
    x1, y1 = norm_to_px(parts[0], parts[1], width, height, "norm1000")
    x2, y2 = norm_to_px(parts[2], parts[3], width, height, "norm1000")
    x1, x2 = max(0, min(x1, width - 1)), max(1, min(x2, width))
    y1, y2 = max(0, min(y1, height - 1)), max(1, min(y2, height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"无效裁切范围: {(x1, y1, x2, y2)}")
    return x1, y1, x2, y2


def cmd_grid(args: argparse.Namespace) -> None:
    """叠加 0..1000 坐标网格便于人工/Agent 精准读取轮廓顶点。"""
    img = Image.open(args.input).convert("RGB")
    w, h = img.size
    cx1, cy1, cx2, cy2 = parse_crop(args.crop, w, h)

    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    step = 20 if args.crop else 50
    major = 100 if not args.crop else 50

    for v in range(0, 1001, step):
        gx = int(round(v * w / 1000.0))
        gy = int(round(v * h / 1000.0))
        is_major = (v % major == 0)
        color = (255, 220, 0) if is_major else (0, 210, 255)
        lw = 2 if is_major else 1
        if cx1 <= gx <= cx2:
            draw.line([(gx, cy1), (gx, cy2)], fill=color, width=lw)
        if cy1 <= gy <= cy2:
            draw.line([(cx1, gy), (cx2, gy)], fill=color, width=lw)

    cropped = canvas.crop((cx1, cy1, cx2, cy2))
    if args.crop and max(cropped.size) < 900:
        scale = max(2, int(round(900 / max(cropped.size))))
        cropped = cropped.resize((cropped.width * scale, cropped.height * scale), Image.NEAREST)
    else:
        scale = 1

    # 在裁切后画布上绘制清晰坐标标签，避免缩放后字迹模糊或被裁掉
    label_draw = ImageDraw.Draw(cropped)
    for v in range(0, 1001, major):
        gx = int(round(v * w / 1000.0))
        gy = int(round(v * h / 1000.0))
        if cx1 <= gx <= cx2:
            lx = int((gx - cx1) * scale)
            label_draw.text((lx + 3, 4), str(v), fill=(255, 230, 0))
        if cy1 <= gy <= cy2:
            ly = int((gy - cy1) * scale)
            label_draw.text((4, ly + 2), str(v), fill=(255, 230, 0))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(out_path, quality=95)
    print(json.dumps({"ok": True, "output": str(out_path), "origSize": [w, h], "cropPx": [cx1, cy1, cx2, cy2]}, ensure_ascii=False))


def draw_round_stroke(draw: ImageDraw.ImageDraw, pts: list[tuple[int, int]], width_px: int) -> None:
    """绘制带圆头端点与拐角的画笔路径。"""
    if not pts:
        return
    r = max(1, width_px // 2)
    if len(pts) == 1:
        x, y = pts[0]
        draw.ellipse([x - r, y - r, x + r, y + r], fill=255)
        return
    draw.line(pts, fill=255, width=max(1, width_px), joint="curve")
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=255)


def build_mask(width: int, height: int, spec: dict[str, Any]) -> np.ndarray:
    """根据多边形与笔刷路径构建紧密贴合轮廓的布尔遮罩。"""
    space = spec.get("coord_space", "norm1000")
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)

    for poly in spec.get("polygons", []):
        if len(poly) < 3:
            continue
        pts = [norm_to_px(float(p[0]), float(p[1]), width, height, space) for p in poly]
        draw.polygon(pts, fill=255)

    for stroke in spec.get("strokes", []):
        raw_pts = stroke.get("points", [])
        if not raw_pts:
            continue
        pts = [norm_to_px(float(p[0]), float(p[1]), width, height, space) for p in raw_pts]
        draw_round_stroke(draw, pts, int(stroke.get("width_px", 12)))

    pad_px = max(0, int(spec.get("pad_px", 2)))
    if pad_px > 0:
        # 仅做极轻微外扩，避免把周围需要给 Seedance 参考的光影/衣领一起遮死
        k = pad_px * 2 + 1
        if k % 2 == 0:
            k += 1
        mask = mask.filter(ImageFilter.MaxFilter(k))

    smooth_px = max(0.0, float(spec.get("smooth_px", 1.5)))
    if smooth_px > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=smooth_px))
        return np.array(mask) >= 110
    return np.array(mask) > 0


def cmd_apply(args: argparse.Namespace) -> None:
    """将指定局部掩码涂白为纯 #FFFFFF，未遮罩像素保持原样。"""
    if bool(args.spec) == bool(args.json):
        raise SystemExit("必须且只能提供 --spec <mask.json> 或 --json '<json>' 其一")
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.spec else json.loads(args.json)

    img = Image.open(args.input).convert("RGB")
    w, h = img.size
    mask_bool = build_mask(w, h, spec)
    white_px = int(mask_bool.sum())
    if white_px == 0:
        raise SystemExit("生成的遮罩为空，请检查 polygons / strokes 坐标。")

    out_np = np.array(img).copy()
    out_np[mask_bool] = [255, 255, 255]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 推荐输出无损 PNG，防止纯白边缘出现 JPEG 蚊式噪点
    if out_path.suffix.lower() in (".jpg", ".jpeg"):
        Image.fromarray(out_np).save(out_path, quality=98)
    else:
        Image.fromarray(out_np).save(out_path, optimize=True)

    ys, xs = np.where(mask_bool)
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(out_path),
                "size": [w, h],
                "whitePixels": white_px,
                "coverageRatio": round(white_px / float(w * h), 4),
                "bboxPx": bbox,
                "fileBytes": out_path.stat().st_size,
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="构图图最小必要纯白 (#FFFFFF) 局部涂白工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_grid = sub.add_parser("grid", help="输出带 0..1000 网格的参考图用于精确定位")
    p_grid.add_argument("-i", "--input", required=True, help="输入图片路径")
    p_grid.add_argument("-o", "--output", required=True, help="输出网格辅助图路径")
    p_grid.add_argument("--crop", help="可选裁切区域 x1,y1,x2,y2（0..1000）以放大检查小人物")
    p_grid.set_defaults(func=cmd_grid)

    p_apply = sub.add_parser("apply", help="将 mask.json 指定局部涂白为 #FFFFFF")
    p_apply.add_argument("-i", "--input", required=True, help="原构图图路径")
    p_apply.add_argument("-o", "--output", required=True, help="输出涂白后图片路径（建议 .png）")
    p_apply.add_argument("--spec", help="mask JSON 文件路径")
    p_apply.add_argument("--json", help="直接传入 JSON 字符串")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
