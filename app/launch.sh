#!/bin/zsh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -z "${WASH_WORKSPACE_ROOT:-}" ]]; then
  if [[ "$(basename "$(dirname "$APP_DIR")")" == "tools" ]]; then
    export WASH_WORKSPACE_ROOT="$(cd "$APP_DIR/../.." && pwd)"
  else
    export WASH_WORKSPACE_ROOT="$HOME/Documents/洗片控制台工作区"
  fi
fi
NO_BROWSER=0
if [[ "${1:-}" == "--no-browser" ]]; then
  NO_BROWSER=1
fi

DATA_DIR="${WASH_DATA_ROOT:-$WASH_WORKSPACE_ROOT/wash_queue_data}"
PY_BIN="${WASH_PYTHON_BIN:-}"
if [[ -z "$PY_BIN" ]]; then
  if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
    PY_BIN="$APP_DIR/.venv/bin/python"
  elif [[ -x "$APP_DIR/../runtime/bin/python3.12" ]]; then
    PY_BIN="$APP_DIR/../runtime/bin/python3.12"
  elif [[ -x "$HOME/.wash_console/venv_x64/bin/python" ]]; then
    PY_BIN="$HOME/.wash_console/venv_x64/bin/python"
  else
    PY_BIN="python3"
  fi
fi
PORT="${WASH_APP_PORT:-8787}"
URL="http://127.0.0.1:${PORT}"

export PATH="$HOME/.local/bin:$HOME/.libtv:$APP_DIR/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export WASH_APP_PORT="$PORT"
export WASH_DATA_ROOT="$DATA_DIR"

mkdir -p "$DATA_DIR"

if ! /usr/bin/curl -fsS "${URL}/api/health" >/dev/null 2>&1; then
  cd "$APP_DIR"
  nohup "$PY_BIN" -m uvicorn server:app --host 127.0.0.1 --port "$PORT" \
    >"$DATA_DIR/server.log" 2>&1 </dev/null &
  for _ in {1..30}; do
    if /usr/bin/curl -fsS "${URL}/api/health" >/dev/null 2>&1; then
      break
    fi
    /bin/sleep 0.2
  done
fi

/usr/bin/curl -fsS -X POST "${URL}/api/agent/bridge/start" >/dev/null 2>&1 || true

if [[ "$NO_BROWSER" == "0" ]]; then
  CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  if [[ -x "$CHROME_BIN" ]]; then
    nohup "$CHROME_BIN" --app="$URL" --window-size=1360,900 >/dev/null 2>&1 </dev/null &
  else
    /usr/bin/open "$URL"
  fi
fi
