#!/usr/bin/env bash
# Run ONE isolated Antigravity bridge instance in the foreground.
# Suitable as the ExecStart of a systemd --user service (one unit per account).
#
# Reads the per-account API key from bridge/keys.json (chmod 600) and sets
# HOME to the account dir so the bridge only sees that account's token.
# No secrets live in this file.
#
# Usage: run_instance.sh <account> [port]
#   account  e.g. oauth1 (a subdirectory of $HAGI_ACCOUNTS_DIR, a key in keys.json)
#   port     default: 8100 + the numeric suffix of the account (oauth1 -> 8101)
#
# Environment overrides:
#   HAGI_ACCOUNTS_DIR      default: ~/.local/share/antigravity-bridge-accounts
#   HAGI_BRIDGE_BIND_HOST  default: 127.0.0.1
#   PYTHON_BIN             default: repo .venv, else python3
set -euo pipefail

ACCOUNT="${1:-}"
if [[ -z "$ACCOUNT" ]]; then
  echo "usage: run_instance.sh <account> [port]" >&2
  exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYS_FILE="$REPO_DIR/bridge/keys.json"
ACCOUNTS_DIR="${HAGI_ACCOUNTS_DIR:-$HOME/.local/share/antigravity-bridge-accounts}"
HOME_DIR="$ACCOUNTS_DIR/$ACCOUNT"
BIND_HOST="${HAGI_BRIDGE_BIND_HOST:-127.0.0.1}"

if [[ -n "${2:-}" ]]; then
  PORT="$2"
else
  n="${ACCOUNT#oauth}"
  if [[ "$n" =~ ^[0-9]+$ ]]; then PORT=$((8100 + n)); else PORT=8100; fi
fi

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$REPO_DIR/.venv/bin/python3" ]]; then
    PYTHON_BIN="$REPO_DIR/.venv/bin/python3"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

if [[ ! -f "$KEYS_FILE" ]]; then echo "ERROR: $KEYS_FILE missing (see bridge/keys.example.json)" >&2; exit 1; fi
KEY=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$KEYS_FILE" "$ACCOUNT")

export HOME="$HOME_DIR"
cd "$REPO_DIR"
exec "$PYTHON_BIN" -u -m bridge.server \
  --host "$BIND_HOST" --port "$PORT" \
  --api-key "$KEY" --home "$HOME_DIR"
