[English](README.md) | **Tiếng Việt**

# Antigravity OAuth Bridge

Cầu nối (bridge) chạy local, phục vụ các model mà tài khoản **Google
Antigravity** của bạn được dùng (Gemini, Claude và GPT-OSS qua Google Code
Assist) thông qua API HTTP chuẩn **tương thích OpenAI** và **tương thích
Anthropic** — để các công cụ như Claude Code, LiteLLM, OpenAI SDK, hay bất kỳ
agent/client local nào cũng dùng được qua `localhost`.

Mỗi instance của bridge gắn chặt với **đúng một** tài khoản Antigravity,
cách ly trong thư mục `HOME` riêng. Bạn có thể chạy một instance duy nhất,
hoặc một cụm nhiều instance (mỗi tài khoản Google một instance) song song,
mỗi instance có token, quota và API key riêng.

> **Dự án không chính thức.** Không liên quan tới Google hay Anthropic. Bạn
> dùng với tài khoản Antigravity của chính mình, tự chịu rủi ro, và có trách
> nhiệm tuân thủ điều khoản dịch vụ của Google đối với tài khoản của bạn.

---

## Tính năng

- **API song chuẩn (dual-stack)** — OpenAI `POST /v1/chat/completions` +
  `GET /v1/models`, và Anthropic `POST /v1/messages`, trên cùng một cổng.
- **Streaming** — Stream SSE trên cả hai chuẩn.
- **Tool use** — Schema function/tool của OpenAI được dịch sang định dạng
  Gemini/Code Assist, tự động làm sạch các tính năng schema mà Code Assist
  từ chối (`oneOf`, `anyOf`, `additionalProperties`, …) và xử lý thought
  signature của Gemini 3.
- **Instance cách ly theo tài khoản** — instance chỉ đọc OAuth token từ
  `HOME` của chính nó (`~/.gemini/antigravity-cli/antigravity-oauth-token`,
  file do Antigravity CLI `agy` ghi ra). Không dò tìm token chéo tài khoản,
  không sao chép token đi nơi khác.
- **Xác thực API key nghiêm ngặt** — mọi request phải trình đúng key của
  instance (`Authorization: Bearer <key>` hoặc `x-api-key`). Key giữ chỗ
  (`"not-needed"`, `"any-key"`, …), key sai và request không key đều nhận
  **401** — kể cả từ loopback hay máy trong LAN.
- **Tự động làm mới token** — Access token được làm mới ~120 giây trước khi
  hết hạn bằng refresh token đã lưu, và ghi trở lại đúng file của instance.
- **Ánh xạ lỗi trung thực** — Lỗi từ upstream giữ nguyên status thật:
  `429` (hết quota) và `403` (tài khoản bị vô hiệu) được truyền thẳng cho
  client thay vì bị ép thành `500` chung chung.
- **Sàn output token** — `maxOutputTokens` được nâng lên tối thiểu 1024, vì
  các model tiered-thinking tiêu ngân sách vào phần suy luận trước; ngân
  sách quá nhỏ do client gửi sẽ khiến upstream trả HTTP 200 nhưng nội dung
  rỗng.
- **Body request lớn** — Chấp nhận payload tới 64 MB.

## Danh sách model

Các model ID mà bridge chấp nhận (alias được ánh xạ sang model Code Assist
tương ứng trong `bridge/client.py`):

| Model ID | Tên hiển thị |
|---|---|
| `gemini-3.8-flash` | Gemini 3.8 Flash (Tiered Thinking) |
| `gemini-3.8-flash-tiered` | Gemini 3.8 Flash Tiered (Thinking) |
| `gemini-3.8-flash-high` | Gemini 3.8 Flash High |
| `gemini-3.8-flash-medium` | Gemini 3.8 Flash Medium |
| `gemini-3.8-flash-low` | Gemini 3.8 Flash Low |
| `gemini-3.7-flash` | Gemini 3.7 Flash (do Gemini 3.8 Flash tiered phục vụ) |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 (Thinking) |
| `claude-opus-4-6` | Claude Opus 4.6 (do Claude Sonnet 4.6 phục vụ) |
| `gpt-oss-120b` | GPT-OSS 120B (do Gemini 3.8 Flash tiered phục vụ) |

Chỉ những model đã kiểm chứng còn tồn tại ở upstream mới được liệt kê.
Các tên Google đã gỡ khỏi Code Assist (Gemini 4 Argon, Gemini 3.7 Flash
High, Gemini 3.6 Flash, Gemini 3.1 Pro — đều trả 404 khi đo live) không
còn trong bảng; request dùng các tên cũ đó sẽ fallback về
`gemini-3.8-flash-tiered` kèm cảnh báo trong log của bridge.

Model nào dùng được còn tùy quyền lợi của tài khoản Antigravity của bạn.

## Yêu cầu

- Python 3.10+
- **Antigravity CLI** (`agy`), đã đăng nhập ít nhất một lần — bridge dùng
  lại file OAuth token mà CLI ghi ra. Nếu chưa có, xem mục ngay dưới đây.
- `pip install -r requirements.txt`

## Cài đặt Antigravity CLI (`agy`)

Bridge không tự đăng nhập Google — nó dùng lại OAuth token mà Antigravity
CLI chính thức ghi ra sau khi bạn đăng nhập. Hãy cài CLI trước.
**Hướng dẫn này chỉ dành cho Linux** (nền tảng bridge được phát triển và
kiểm chứng):

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Trình cài đặt đặt `agy` vào `~/.local/bin` (dùng `--dir <path>` nếu muốn
đổi vị trí). Hãy chắc chắn thư mục đó nằm trong `PATH`, rồi mở lại terminal.

**Kiểm tra cài đặt và đăng nhập**

```bash
agy --version        # phải in ra phiên bản, ví dụ 1.3.x
agy -p "hi"          # lần chạy đầu sẽ mở trình duyệt để đăng nhập Google
```

Đăng nhập xong, OAuth token được lưu tại
`~/.gemini/antigravity-cli/antigravity-oauth-token`.
File này là thông tin đăng nhập duy nhất bridge cần — bridge đọc nó từ
`HOME` của instance mà nó chạy, và tự động làm mới khi sắp hết hạn.

> macOS / Windows: có trình cài đặt tại <https://antigravity.google>,
> nhưng nằm ngoài hướng dẫn này và chưa được kiểm chứng với bridge
> (script khởi động và unit systemd trong repo chỉ dành cho Linux;
> trên Windows bạn sẽ cần WSL).

## Bắt đầu nhanh — một instance

```bash
git clone https://github.com/chauvuusvn/antigravity-bridge.git
cd antigravity-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. Đăng nhập bằng Antigravity CLI một lần (tạo file token trong $HOME)
agy -p "hi"

# 2. Chọn một API key cho bridge (chuỗi ngẫu nhiên dài bất kỳ) và khởi động
export ANTIGRAVITY_BRIDGE_API_KEY="$(openssl rand -hex 24)"
.venv/bin/python -m bridge.server --host 127.0.0.1 --port 8100 \
    --api-key-env ANTIGRAVITY_BRIDGE_API_KEY --home "$HOME"

# 3. Gọi như một OpenAI API
curl http://127.0.0.1:8100/v1/chat/completions \
  -H "Authorization: Bearer $ANTIGRAVITY_BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Hello!"}]}'
```

Kiểm tra sức khỏe: `GET /health` trả về trạng thái và phiên bản của bridge.

## Cụm nhiều tài khoản (tùy chọn)

Chạy một instance cách ly cho mỗi tài khoản Google — hữu ích khi các công
việc khác nhau không nên dùng chung quota của một tài khoản.

1. Tạo thư mục home cho từng tài khoản và đăng nhập CLI dưới mỗi thư mục:

   ```bash
   export ACCOUNTS=~/.local/share/antigravity-bridge-accounts
   mkdir -p "$ACCOUNTS/oauth1"
   HOME="$ACCOUNTS/oauth1" agy -p "hi"   # đăng nhập bằng tài khoản 1
   # lặp lại với oauth2, oauth3, ... cho các tài khoản tiếp theo
   ```

2. Tạo `bridge/keys.json` (chmod 600) với một API key ngẫu nhiên cho mỗi
   tài khoản — xem mẫu `bridge/keys.example.json`:

   ```bash
   cp bridge/keys.example.json bridge/keys.json
   # sửa các giá trị key, rồi:
   chmod 600 bridge/keys.json
   ```

3. Khởi động cả cụm:

   ```bash
   bridge/launch_all.sh
   ```

   Theo quy ước, tài khoản `oauthN` được phục vụ ở cổng `8100 + N`
   (`oauth1` → 8101, `oauth2` → 8102, …). Có thể đổi bằng các biến môi
   trường ghi ở đầu file `bridge/launch_all.sh` (`HAGI_ACCOUNTS_DIR`,
   `HAGI_BASE_PORT`, `HAGI_BRIDGE_BIND_HOST`, `PYTHON_BIN`).

4. Hoặc chạy một instance ở foreground (cũng là entry point mà template
   systemd sử dụng):

   ```bash
   bridge/run_instance.sh oauth1
   ```

### systemd (user service, mỗi tài khoản một unit)

```bash
mkdir -p ~/.config/systemd/user
cp antigravity-bridge@.service ~/.config/systemd/user/
# chỉnh WorkingDirectory trong unit nếu thư mục clone không phải ~/antigravity-bridge
systemctl --user daemon-reload
systemctl --user enable --now antigravity-bridge@oauth1
```

## Dùng với các client

**OpenAI SDK / mọi client tương thích OpenAI**

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8101/v1", api_key="<instance key>")
print(client.chat.completions.create(
    model="gemini-3.8-flash",
    messages=[{"role": "user", "content": "Hello!"}]).choices[0].message.content)
```

**Claude Code (chuẩn Anthropic)**

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8101"
export ANTHROPIC_API_KEY="<instance key>"
claude
```

Adapter Anthropic (`bridge/anthropic_adapter.py`) dịch request
`/v1/messages` sang dạng OpenAI ở bên trong; tên model bạn truyền vào được
chuyển tiếp sang bảng ánh xạ model của bridge.

**LiteLLM** — xem `claude_litellm_config.yaml` để có cấu hình mẫu ánh xạ
tên model kiểu Anthropic lên bridge.

## Mô hình bảo mật

- Bind vào `127.0.0.1` (mặc định ở mọi nơi trong repo này). Chỉ mở cổng ra
  interface rộng hơn khi máy được bảo vệ bằng firewall hoặc mạng riêng mà
  bạn tin cậy — ai giữ key của instance là dùng được quota của tài khoản đó.
- Một instance = một tài khoản = một key. Key chỉ nằm trong
  `bridge/keys.json` (đã git-ignore, `chmod 600`) hoặc trong biến môi
  trường bạn truyền qua `--api-key-env`.
- Token không bao giờ rời khỏi `HOME` của instance; cơ chế auto-refresh ghi
  trở lại đúng file token mà Antigravity CLI đã tạo.
- Không telemetry, không gọi bên thứ ba: bridge chỉ nói chuyện với các
  endpoint OAuth và Code Assist của Google.

## Cấu trúc dự án

```
bridge/
  server.py             HTTP server (chuẩn OpenAI + Anthropic, auth, health)
  client.py             Dịch OpenAI <-> Code Assist, streaming, tools
  anthropic_adapter.py  Dịch Anthropic /v1/messages <-> OpenAI
  auth.py               Nạp token theo instance + auto-refresh
  launch_all.sh         Khởi động toàn bộ cụm nhiều tài khoản
  run_instance.sh       Khởi động một instance ở foreground (entry cho systemd)
  keys.example.json     Mẫu cho bridge/keys.json
tests/                  Bộ test pytest (cách ly auth, refresh, hardening, schema)
antigravity-bridge@.service   Template unit systemd --user
claude_litellm_config.yaml    Cấu hình LiteLLM mẫu
README.md                     Tài liệu tiếng Anh
```

## Chạy test

```bash
pip install pytest
python -m pytest tests/ -q
```

## Tuyên bố miễn trừ

Đây là công cụ tương tác không chính thức, dùng với tài khoản Antigravity
**của chính bạn**. Phần mềm được cung cấp nguyên trạng, không bảo hành.
Việc dùng tài khoản Antigravity consumer theo cách tự động/lập trình có
thể xung đột với điều khoản của Google — bạn chịu trách nhiệm về cách
mình sử dụng.

## Giấy phép

MIT — xem `LICENSE`.
