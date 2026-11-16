#!/usr/bin/env bash
# Launch a cluster of isolated Antigravity bridge instances, one per account.
#
# Each account lives in its own HOME directory under ACCOUNTS_DIR, so every
# instance sees exactly one Antigravity login token and one quota pool.
# Per-instance API keys are read from bridge/keys.json (chmod 600, never
# committed — see bridge/keys.example.json).
#
# Environment overrides:
#   HAGI_ACCOUNTS_DIR      base dir with one subdir per account
#                          (default: ~/.local/share/antigravity-bridge-accounts)
#   HAGI_BRIDGE_BIND_HOST  bind address (default: 127.0.0.1 — keep it local;
#                          only set 0.0.0.0 if you understand the risk and
#                          protect the ports with a firewall / private network)
#   HAGI_BASE_PORT         port of the first account (default: 8101;
#                          account oauthN uses BASE_PORT+N-1)
#   PYTHON_BIN             python interpreter (default: repo .venv, else python3)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYS_FILE="$REPO_DIR/bridge/keys.json"
ACCOUNTS_DIR="${HAGI_ACCOUNTS_DIR:-$HOME/.local/share/antigravity-bridge-accounts}"
BASE_PORT="${HAGI_BASE_PORT:-8101}"
BIND_HOST="${HAGI_BRIDGE_BIND_HOST:-127.0.0.1}"

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_DIR/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

if [[ ! -f "$KEYS_FILE" ]]; then
  echo "ERROR: $KEYS_FILE missing. Create it with chmod 600, for example:" >&2
  echo '  {"oauth1":"<key1>","oauth2":"<key2>"}   (see bridge/keys.example.json)' >&2
  exit 1
fi

# Accounts are discovered from keys.json, in order. Account "oauthN" is
# served on port BASE_PORT + N - 1 and uses $ACCOUNTS_DIR/oauthN as HOME.
mapfile -t ACCOUNTS < <(python3 -c "import json; print('\n'.join(json.load(open('$KEYS_FILE')).keys()))")

port_for() {
  local acct="$1" n
  n="${acct#oauth}"
  if [[ "$n" =~ ^[0-9]+$ ]]; then
    echo $((BASE_PORT + n - 1))
  else
    echo "$BASE_PORT"
  fi
}

key_for() {
  python3 -c "import json,sys; print(json.load(open('$KEYS_FILE')).get(sys.argv[1],''))" "$1"
}

for acct in "${ACCOUNTS[@]}"; do
  home="$ACCOUNTS_DIR/$acct"
  port="$(port_for "$acct")"
  key="$(key_for "$acct")"
  if [[ -z "$key" ]]; then echo "WARN: no key for $acct, skip"; continue; fi
  if [[ ! -d "$home" ]]; then echo "WARN: $home missing, skip $acct"; continue; fi

  if curl -s -o /dev/null "http://127.0.0.1:$port/health" 2>/dev/null; then
    echo "NOTE: port $port already serving; skipping launch for $acct"
    continue
  fi

  echo "Launching $acct on $BIND_HOST:$port (HOME=$home)"
  (cd "$REPO_DIR" && exec nohup env HOME="$home" PYTHONPATH="$REPO_DIR" \
    ANTIGRAVITY_BRIDGE_API_KEY="$key" "$PYTHON_BIN" -u -m bridge.server \
    --host "$BIND_HOST" --port "$port" --api-key-env ANTIGRAVITY_BRIDGE_API_KEY --home "$home" \
    > "$REPO_DIR/bridge/$acct.log" 2>&1) &
  echo "  pid=$!"
  disown "$!" 2>/dev/null || true
done

echo "Done. Health check:"
for acct in "${ACCOUNTS[@]}"; do
  port="$(port_for "$acct")"
  key="$(key_for "$acct")"
  code="000"
  for attempt in {1..10}; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $key" "http://127.0.0.1:$port/health" || true)
    [[ "$code" == "200" ]] && break
    sleep 1
  done
  echo "  $acct ($port): health=$code"
done
