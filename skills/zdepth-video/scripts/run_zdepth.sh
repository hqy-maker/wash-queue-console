#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/video_to_depth.py"

# 自动定位 uv（支持 PATH、~/.local/bin、~/.cargo/bin）
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
elif [ -x "${HOME}/.local/bin/uv" ]; then
  UV_BIN="${HOME}/.local/bin/uv"
elif [ -x "${HOME}/.cargo/bin/uv" ]; then
  UV_BIN="${HOME}/.cargo/bin/uv"
fi

if [ -n "${UV_BIN}" ]; then
  # 若本地已缓存全部依赖，--offline 秒启动；首次使用未缓存时自动回退在线拉取并缓存
  if "${UV_BIN}" run --offline "${PY_SCRIPT}" --help >/dev/null 2>&1; then
    exec "${UV_BIN}" run --offline "${PY_SCRIPT}" "$@"
  else
    echo "[zdepth-video] 首次运行正在自动配置 Python 依赖环境（后续运行将离线秒开）..."
    exec "${UV_BIN}" run "${PY_SCRIPT}" "$@"
  fi
fi

# 未安装 uv 时回退至本机 Python 环境
if command -v python3 >/dev/null 2>&1; then
  exec python3 "${PY_SCRIPT}" "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "${PY_SCRIPT}" "$@"
else
  echo "❌ 未检测到 uv 或 python3。建议先执行: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi
