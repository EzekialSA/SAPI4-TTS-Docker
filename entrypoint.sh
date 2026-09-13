#!/bin/bash
# sapi4-tts entrypoint: Xvfb -> warm wineserver -> Wyoming + FastAPI.
set -euo pipefail

export WINEPREFIX="${WINEPREFIX:-/opt/sapi4/wine}"
export WINEARCH=win32
export WINEDEBUG="${WINEDEBUG:--all}"
export DISPLAY="${DISPLAY:-:99}"

cleanup() {
    echo "[entrypoint] shutting down"
    kill "${API_PID:-}" "${WYO_PID:-}" 2>/dev/null || true
    wineserver -k 2>/dev/null || true
    kill "${XVFB_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[entrypoint] starting Xvfb on $DISPLAY"
Xvfb "$DISPLAY" -screen 0 1024x768x16 -nolisten tcp >/dev/null 2>&1 &
XVFB_PID=$!
sleep 2

# Keep wineserver resident. Without this every request pays full prefix startup.
echo "[entrypoint] warming wineserver"
wineserver -p 2>/dev/null || true
wine wineboot -u >/dev/null 2>&1 || true
sleep 3

cd /opt/sapi4/app

echo "[entrypoint] verifying voices"
if ! (cd /opt/sapi4/bin && wine sapi4limits.exe 2>/dev/null | tr -d '\r' | grep -q .); then
    echo "[entrypoint] WARNING: no voices enumerated" >&2
fi

if [ "${ENABLE_WYOMING:-1}" = "1" ]; then
    echo "[entrypoint] starting Wyoming on ${WYOMING_URI:-tcp://0.0.0.0:10200}"
    python3 wyoming_server.py --uri "${WYOMING_URI:-tcp://0.0.0.0:10200}" &
    WYO_PID=$!
fi

echo "[entrypoint] starting HTTP API on :5000"
exec uvicorn server:app --host 0.0.0.0 --port 5000 --workers 1 &
API_PID=$!

wait -n "${API_PID}" "${WYO_PID:-$API_PID}"
