#!/usr/bin/env bash
# Launch a cluster of isolated Antigravity bridge instances, one per account.
#
# Each account lives in its own HOME directory under ACCOUNTS_DIR, so every
# instance sees exactly one Antigravity login token and one quota pool.
# Per-instance API keys are read from bridge/keys.json (chmod 600, never
# committed — see bridge/keys.example.json).
#
# Environment overrides:
#   ANTIGRAVITY_BRIDGE_ACCOUNTS_DIR  base dir with one subdir per account
#                          (default: ~/.local/share/antigravity-bridge-accounts)
#   ANTIGRAVITY_BRIDGE_BIND_HOST     bind address (default: 127.0.0.1 — keep
#                          it local; only set 0.0.0.0 if you understand the
#                          risk and protect the ports with a firewall /
#                          private network)
#   ANTIGRAVITY_BRIDGE_BASE_PORT     port of the first account (default:
#                          8101; account oauthN uses BASE_PORT+N-1)
#   PYTHON_BIN             python interpreter (default: repo .venv, else python3)
# The legacy HAGI_* spellings are still honoured as fallbacks.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYS_FILE="$REPO_DIR/bridge/keys.json"
ACCOUNTS_DIR="${ANTIGRAVITY_BRIDGE_ACCOUNTS_DIR:-${HAGI_ACCOUNTS_DIR:-$HOME/.local/share/antigravity-bridge-accounts}}"
BASE_PORT="${ANTIGRAVITY_BRIDGE_BASE_PORT:-${HAGI_BASE_PORT:-8101}}"
BIND_HOST="${ANTIGRAVITY_BRIDGE_BIND_HOST:-${HAGI_BRIDGE_BIND_HOST:-127.0.0.1}}"

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
# A malformed keys.json must fail loudly here — an empty account list
# previously produced a silent "Done" with nothing running.
if ! ACCOUNTS_RAW="$(python3 -c "import json,sys; d=json.load(open('$KEYS_FILE')); assert isinstance(d, dict) and d, 'keys.json must be a non-empty object'; print('\n'.join(d.keys()))" 2>&1)"; then
  echo "ERROR: cannot read accounts from $KEYS_FILE: $ACCOUNTS_RAW" >&2
  exit 1
fi
mapfile -t ACCOUNTS <<< "$ACCOUNTS_RAW"

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

declare -A SEEN_PORTS=()
for acct in "${ACCOUNTS[@]}"; do
  # Account names become directory paths and log file names: only a safe
  # charset is allowed, and path traversal is rejected outright.
  if [[ ! "$acct" =~ ^[A-Za-z0-9._-]+$ || "$acct" == *".."* ]]; then
    echo "ERROR: unsafe account name '$acct' in keys.json, skip" >&2
    continue
  fi
  home="$ACCOUNTS_DIR/$acct"
  port="$(port_for "$acct")"
  if [[ -n "${SEEN_PORTS[$port]:-}" ]]; then
    echo "ERROR: accounts '${SEEN_PORTS[$port]}' and '$acct' both map to port $port (name accounts oauthN to get distinct ports), skip $acct" >&2
    continue
  fi
  SEEN_PORTS[$port]="$acct"
  key="$(key_for "$acct")"
  if [[ -z "$key" ]]; then echo "WARN: no key for $acct, skip"; continue; fi
  if [[ ! -d "$home" ]]; then echo "WARN: $home missing, skip $acct"; continue; fi

  # "Already serving" must mean THIS bridge, not any HTTP service that
  # happens to answer 200 on the port: check the /health body marker.
  if curl -s "http://127.0.0.1:$port/health" 2>/dev/null | grep -q '"bridge": "antigravity"'; then
    echo "NOTE: port $port already serving this bridge; skipping launch for $acct"
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
