#!/bin/bash
# Include Homebrew ffmpeg when launched from Finder.
cd "$(dirname "$0")" || exit 1
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
python_bin=".venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "The project Python environment is missing. Follow Quickstart in README.md."
  exit 1
fi
mkdir -p ui/logs || exit 1
if [[ -n "${PROXYDIRECTOR_PORT:-}" ]]; then
  ports=("$PROXYDIRECTOR_PORT")
else
  ports=(4217 4207 4218 4219 4220)
fi

is_studio() {
  curl --noproxy '*' --silent --fail --max-time 1 "http://127.0.0.1:$1/api/health" |
    "$python_bin" -c 'import json,sys
try:
    d=json.load(sys.stdin)
except (ValueError, OSError):
    sys.exit(1)
sys.exit(not (isinstance(d, dict) and d.get("service") == "proxydirector" and d.get("version") == "studio-202611-en"))'
}

open_studio() {
  local url="http://127.0.0.1:$1"
  echo "ProxyDirector is ready at $url"
  open "$url"
}

for port in "${ports[@]}"; do
  if is_studio "$port"; then
    open_studio "$port"
    exit $?
  fi
done

for port in "${ports[@]}"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    continue
  fi
  log="ui/logs/studio-$port.log"
  PROXYDIRECTOR_PORT="$port" nohup "$python_bin" ui/server.py </dev/null >>"$log" 2>&1 &
  server_pid=$!
  for ((attempt=0; attempt<50; attempt++)); do
    if is_studio "$port"; then
      open_studio "$port"
      exit $?
    fi
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 0.25
  done
  echo "Could not start on port $port. See $log."
done
echo "Could not find a free port for ProxyDirector."
exit 1
