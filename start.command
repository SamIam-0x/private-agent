#!/bin/zsh
# Double-click to use the private agent.
# Starts the Tinfoil container if it is stopped, opens the chat page, and stops
# the container again when this window is closed (or Ctrl+C), so it only bills
# while you use it.

unset PYTHONPATH PYTHONHOME
TINFOIL="$HOME/.local/bin/tinfoil"
NAME="private-agent"
LOG="$HOME/Library/Logs/private-agent-launcher.log"
cd "$HOME/Documents/GitHub/private-agent" || { echo "private-agent folder not found"; read; exit 1; }

if lsof -nP -iTCP:8767 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Already open in another window. Opening the page."
  open "http://127.0.0.1:8767/"
  exit 0
fi

container_status() { "$TINFOIL" container get "$NAME" 2>/dev/null | awk '/^Status:/{print $2}'; }

stop_container() {
  trap - EXIT HUP INT TERM
  echo "$(date '+%F %T') stopping $NAME" >> "$LOG"
  # Detached so it still runs after the window is gone.
  nohup "$TINFOIL" container stop "$NAME" >> "$LOG" 2>&1 &!
  echo "Stopping the container. You can close this window."
  exit 0
}

state=$(container_status)
if [[ "$state" != "Running" ]]; then
  echo "Starting the container (about 2 minutes)..."
  echo "$(date '+%F %T') deploying $NAME (was: ${state:-unknown})" >> "$LOG"
  if ! "$TINFOIL" container deploy "$NAME" 2>&1 | grep -E --line-buffered "Running at|rror|Boot:"; then :; fi
  state=$(container_status)
  if [[ "$state" != "Running" ]]; then
    echo "The container did not start (status: ${state:-unknown}). Details: $LOG"
    read
    exit 1
  fi
fi

echo "Ready. Leave this window open while you chat. Close it to stop the container."
# Run the page in the background and wait, so a closed window (SIGHUP) runs the
# trap right away instead of after the page exits.
.venv/bin/python app.py &
APP=$!
trap 'kill $APP 2>/dev/null; stop_container' EXIT HUP INT TERM
wait $APP
