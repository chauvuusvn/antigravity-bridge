**English** | [Tiếng Việt](README_VI.md)

# Antigravity OAuth Bridge

A local bridge that serves the models available to your **Google Antigravity**
account (Gemini, Claude and GPT-OSS via Google Code Assist) behind standard
**OpenAI-compatible** and **Anthropic-compatible** HTTP APIs — so tools such as
Claude Code, LiteLLM, the OpenAI SDK, or any local agent/client can use them
on `localhost`.

Each bridge instance is bound to **exactly one** Antigravity login, isolated
in its own `HOME` directory. You can run a single instance, or a small cluster
of instances (one per Google account) side by side, each with its own token,
quota and API key.

> **Unofficial project.** Not affiliated with Google or Anthropic. You use it
> with your own Antigravity account, at your own risk, and are responsible for
> complying with Google's terms of service for your account.

---

## Features

- **Dual-stack API** — OpenAI `POST /v1/chat/completions` + `GET /v1/models`,
  and Anthropic `POST /v1/messages`, on the same port.
- **Streaming** — SSE streaming on both stacks.
- **Tool use** — OpenAI function/tool schemas are translated to Gemini/Code
  Assist format, with automatic schema sanitization for features Code Assist
  rejects (`oneOf`, `anyOf`, `additionalProperties`, …) and Gemini 3 thought
  signature handling.
- **Isolated per-account instances** — an instance reads its OAuth token
  *only* from its own `HOME` (`~/.gemini/antigravity-cli/antigravity-oauth-token`,
  the file written by the Antigravity CLI `agy`). No cross-account discovery,
  no token replication anywhere else.
- **Strict API-key auth** — every request must present the instance's own key
  (`Authorization: Bearer <key>` or `x-api-key`). Placeholder keys
  (`"not-needed"`, `"any-key"`, …), wrong keys and missing keys get **401** —
  from loopback and LAN peers alike.
- **Token auto-refresh** — access tokens are refreshed ~120 s before expiry
  using the stored refresh token and written back to the same instance file.
- **Faithful error mapping** — upstream failures keep their real status:
  `429` (quota exhausted) and `403` (account disabled) are passed through
  instead of being flattened into a generic `500`.
- **Output-token floor** — `maxOutputTokens` is raised to at least 1024,
  because tiered-thinking models spend the budget on reasoning first and a
  tiny client budget otherwise returns HTTP 200 with empty content.
- **Large request bodies** — payloads up to 64 MB are accepted.

## Models

Model IDs accepted by the bridge (aliases map to the Code Assist model shown
in `bridge/client.py`):

| Model ID | Display name |
|---|---|
| `gemini-4-argon` | Gemini 4 Argon (Thinking) |
| `argon` | Gemini 4 Argon |
| `gemini-3.8-flash` | Gemini 3.8 Flash (Tiered Thinking) |
| `gemini-3.8-flash-tiered` | Gemini 3.8 Flash Tiered (Thinking) |
| `gemini-3.8-flash-high` | Gemini 3.8 Flash High |
| `gemini-3.8-flash-medium` | Gemini 3.8 Flash Medium |
| `gemini-3.8-flash-low` | Gemini 3.8 Flash Low |
| `gemini-3.7-flash` | Gemini 3.7 Flash High |
| `gemini-3.7-flash-high` | Gemini 3.7 Flash High |
| `gemini-3.6-flash` | Gemini 3.6 Flash Medium |
| `gemini-3.1-pro` | Gemini 3.1 Pro Low |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 (Thinking) |
| `claude-opus-4-6` | Claude Opus 4.6 (Thinking) |
| `gpt-oss-120b` | GPT-OSS 120B (Medium) |

Availability depends on what your Antigravity account is entitled to.

## Requirements

- Python 3.10+
- The **Antigravity CLI** (`agy`), logged in at least once — the bridge reuses
  the OAuth token file the CLI writes. (Install Antigravity from
  <https://antigravity.google>.)
- `pip install -r requirements.txt`

## Quick start — single instance

```bash
git clone https://github.com/chauvuusvn/antigravity-bridge.git
cd antigravity-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. Log in with the Antigravity CLI once (creates the token file in $HOME)
agy -p "hi"

# 2. Choose a bridge API key (any long random string) and start the bridge
export ANTIGRAVITY_BRIDGE_API_KEY="$(openssl rand -hex 24)"
.venv/bin/python -m bridge.server --host 127.0.0.1 --port 8100 \
    --api-key-env ANTIGRAVITY_BRIDGE_API_KEY --home "$HOME"

# 3. Call it like an OpenAI API
curl http://127.0.0.1:8100/v1/chat/completions \
  -H "Authorization: Bearer $ANTIGRAVITY_BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Hello!"}]}'
```

Health check: `GET /health` returns the bridge status and version.

## Multi-account cluster (optional)

Run one isolated instance per Google account — useful when different
workloads should not share one account's quota.

1. Create one home directory per account and log the CLI in under each:

   ```bash
   export ACCOUNTS=~/.local/share/antigravity-bridge-accounts
   mkdir -p "$ACCOUNTS/oauth1"
   HOME="$ACCOUNTS/oauth1" agy -p "hi"   # log in with account 1
   # repeat with oauth2, oauth3, ... for more accounts
   ```

2. Create `bridge/keys.json` (chmod 600) with one random API key per account
   — see `bridge/keys.example.json`:

   ```bash
   cp bridge/keys.example.json bridge/keys.json
   # edit the values, then:
   chmod 600 bridge/keys.json
   ```

3. Launch the cluster:

   ```bash
   bridge/launch_all.sh
   ```

   By convention account `oauthN` is served on port `8100 + N`
   (`oauth1` → 8101, `oauth2` → 8102, …). Override with the environment
   variables documented at the top of `bridge/launch_all.sh`
   (`HAGI_ACCOUNTS_DIR`, `HAGI_BASE_PORT`, `HAGI_BRIDGE_BIND_HOST`,
   `PYTHON_BIN`).

4. Or run a single instance in the foreground (also the entry point used by
   the systemd template):

   ```bash
   bridge/run_instance.sh oauth1
   ```

### systemd (user service, one unit per account)

```bash
mkdir -p ~/.config/systemd/user
cp antigravity-bridge@.service ~/.config/systemd/user/
# adjust WorkingDirectory inside the unit if your clone is not ~/antigravity-bridge
systemctl --user daemon-reload
systemctl --user enable --now antigravity-bridge@oauth1
```

## Using it with clients

**OpenAI SDK / any OpenAI-compatible client**

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8101/v1", api_key="<instance key>")
print(client.chat.completions.create(
    model="gemini-3.8-flash",
    messages=[{"role": "user", "content": "Hello!"}]).choices[0].message.content)
```

**Claude Code (Anthropic stack)**

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8101"
export ANTHROPIC_API_KEY="<instance key>"
claude
```

The Anthropic adapter (`bridge/anthropic_adapter.py`) translates
`/v1/messages` requests to the OpenAI shape internally; the model name you
pass is forwarded to the bridge's model mapping.

**LiteLLM** — see `claude_litellm_config.yaml` for a ready example that maps
Anthropic-style model names onto the bridge.

## Security model

- Bind to `127.0.0.1` (the default everywhere in this repo). Only expose the
  ports on a wider interface if the machine is protected by a firewall or a
  private network you trust — whoever holds an instance key can spend that
  account's quota.
- One instance = one account = one key. Keys live only in `bridge/keys.json`
  (git-ignored, `chmod 600`) or in the environment variable you pass with
  `--api-key-env`.
- Tokens never leave the instance's own `HOME`; auto-refresh writes back to
  the same token file the Antigravity CLI created.
- No telemetry, no third-party calls: the bridge talks only to Google's
  OAuth and Code Assist endpoints.

## Project layout

```
bridge/
  server.py             HTTP server (OpenAI + Anthropic stacks, auth, health)
  client.py             OpenAI <-> Code Assist translation, streaming, tools
  anthropic_adapter.py  Anthropic /v1/messages <-> OpenAI translation
  auth.py               Per-instance token loading + auto-refresh
  launch_all.sh         Start the whole multi-account cluster
  run_instance.sh       Start one instance in the foreground (systemd entry)
  keys.example.json     Template for bridge/keys.json
tests/                  pytest suite (auth isolation, refresh, hardening, schemas)
antigravity-bridge@.service   systemd --user template unit
claude_litellm_config.yaml    LiteLLM example config
README_VI.md                  Vietnamese documentation
```

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

## Disclaimer

This is an unofficial interoperability tool for use with **your own**
Antigravity account. It is provided as-is, without warranty. Automated or
programmatic use of a consumer Antigravity account may conflict with
Google's terms — you are responsible for how you use it.

## License

MIT — see `LICENSE`.
