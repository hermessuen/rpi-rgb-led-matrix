#!/usr/bin/env bash
# Supervisor for the L-train + SMS display: restarts on crash, cleans up any
# orphaned renderer that's still holding the LED matrix. Run inside tmux.
#
#   export NGROK_AUTHTOKEN=<token>
#   ./utils/run-display.sh            # brightness 50
#   BRIGHTNESS=30 ./utils/run-display.sh
set -u

UTILS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$UTILS_DIR")"
cd "$REPO_DIR"

: "${NGROK_AUTHTOKEN:?Set NGROK_AUTHTOKEN (export NGROK_AUTHTOKEN=...)}"
BRIGHTNESS="${BRIGHTNESS:-50}"

# Build the renderer once if it's missing.
if [ ! -x "$UTILS_DIR/train-display" ]; then
  echo "[run-display] building train-display..."
  make -C "$UTILS_DIR" train-display || exit 1
fi

cleanup() {
  # The C++ renderer is a child of python; on a hard crash it can be orphaned
  # and keep holding the matrix, which blocks the next start. Reap it.
  sudo pkill -f 'utils/train-display' 2>/dev/null || true
  sudo pkill -f 'utils/text-scroller' 2>/dev/null || true
}
trap cleanup EXIT

while true; do
  echo "[run-display] starting (brightness=$BRIGHTNESS) at $(date)"
  sudo --preserve-env=NGROK_AUTHTOKEN \
    python3 utils/sms_display.py --brightness "$BRIGHTNESS"
  code=$?
  cleanup
  if [ "$code" -eq 0 ]; then
    echo "[run-display] clean exit; stopping."
    break
  fi
  echo "[run-display] crashed (code $code); restarting in 3s..."
  sleep 3
done
