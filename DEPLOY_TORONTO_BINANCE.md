# Krabs Toronto Binance Deploy Guide For Agents

This file is a handoff for another agent/operator. It explains the current
Toronto deployment, where state is stored, how to restart, which branch to use,
and which mistakes must not be repeated.

Do not paste secrets into chat, Markdown, shell logs, or screenshots.

## Mandatory Binance References

Before planning, coding, debugging, or reviewing any Binance-related change,
read and use these primary references:

```text
https://developers.binance.com/docs/derivatives/
https://demo-fapi.binance.com/
https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info
```

Use them as the source of truth for:

```text
USD-M futures endpoints
Demo Trading behavior
Demo base URLs
Auth requirements
API key environment differences
Permission/IP whitelist checks
```

Do not rely on old Binance Futures Testnet assumptions without checking these
references first.

## Current Toronto State

Server:

```text
Toronto VPS
IPv4: 155.138.140.211
OS: Ubuntu 24.04.x
SSH user: root
```

Credentials are not stored in this file. Read them locally from:

```text
O:\vpn-mur\toronto.txt
O:\vpn-mur\vultr_key
```

Current app directory:

```text
/root/krabs
```

Current service:

```text
krabs.service
```

Expected running command:

```text
/root/krabs/.venv/bin/python /root/krabs/start.py
```

Old similar bot:

```text
krabs4.service
```

Expected state:

```text
krabs.service active
krabs4.service inactive
only one /root/krabs/start.py process
no /opt/krabs4/start.py process
```

Branch deployed:

```text
BinanceTest
origin/BinanceTest
HEAD 22b5fb0
```

Important: the server currently has local hotfix edits on top of
`origin/BinanceTest`:

```text
bot/main.py
bot/exchange/binance_client.py
bot/handlers/assistant.py
bot/handlers/signals.py
bot/signals/
requirements.txt
tests/test_signal_*.py
```

Do not overwrite these by blindly running `git reset --hard` or re-copying the
raw remote branch unless you reapply the hotfixes and signal changes below.

## What This Deploy Is

This server runs the Telegram bot from `tradenaire/krabs` on the Toronto VPS.
For Binance support use the `BinanceTest` branch, not `main`.

Current intended server path:

```text
/root/krabs
```

Current intended systemd service:

```text
krabs.service
```

Old similar bot that must stay stopped:

```text
krabs4.service
```

## What Is Stored On The Server

Application code:

```text
/root/krabs
```

Python virtualenv:

```text
/root/krabs/.venv
```

SQLite state/config database:

```text
/root/krabs/data/bot.db
```

This database stores runtime config such as:

```text
telegram_token
exchange_provider
binance_api_key
binance_secret
mexc_api_key
mexc_secret
allowed_user_ids
trade settings
paper/live positions
```

Bot log:

```text
/root/krabs/data/bot.log
```

Structured logs, if present:

```text
/root/krabs/data/logs/
```

Systemd unit:

```text
/etc/systemd/system/krabs.service
```

Telegram token source on the local Windows machine:

```text
O:\vpn-mur\krabs-signal-bot-token.txt
```

Do not commit `data/`, `.venv/`, tokens, API keys, passwords, or local server
notes into Git.

## Fast Restart Commands

On Toronto:

```bash
systemctl status krabs.service --no-pager
systemctl restart krabs.service
systemctl is-active krabs.service
journalctl -u krabs.service -n 80 --no-pager
```

If the old service appears:

```bash
systemctl stop krabs4.service || true
systemctl disable krabs4.service || true
systemctl is-active krabs4.service || true
```

Check processes:

```bash
ps -eo pid,ppid,lstart,cmd | grep -E "/root/krabs|/opt/krabs|start.py" | grep -v grep || true
```

Expected: one `/root/krabs/.venv/bin/python /root/krabs/start.py` process.

## Access Needed

You need these accesses before deploying:

1. GitHub access to `https://github.com/tradenaire/krabs`
   - At minimum read access for cloning/fetching.
   - Push access only if you want to publish local hotfixes back to GitHub.

2. Toronto VPS SSH access
   - Server details are stored in `O:\vpn-mur\toronto.txt`.
   - SSH key path, if enabled: `O:\vpn-mur\vultr_key`.
   - If key auth does not work, use the root password from `toronto.txt`.

3. Telegram bot token
   - Stored locally in `O:\vpn-mur\krabs-signal-bot-token.txt`.
   - The file contains only the token.
   - Do not paste this token into logs, shell history, screenshots, or Markdown files.

4. Binance Demo Trading API keys
   - For safe test trading, use Binance Demo Trading keys, not old Futures Testnet keys.
   - Create/manage them at:

```text
https://demo.binance.com/en/my/settings/api-management
```

   - Old `testnet.binancefuture.com` API keys are not interchangeable with Demo Trading keys.
   - CCXT no longer supports Binance futures sandbox via `set_sandbox_mode(True)`.
   - The bot must use `enable_demo_trading(True)`.

5. Binance real account API keys, only for live-money trading
   - Create/manage them at:

```text
https://www.binance.com/en/my/settings/api-management
```

   - Use these only with `exchange_provider=binance`.
   - Do not use live keys with `exchange_provider=binance_testnet`.

## Local Setup

Work from:

```powershell
K:\krabs-final
```

Fetch and switch to the Binance branch:

```powershell
git fetch origin
git switch -C BinanceTest --track origin/BinanceTest
```

Check branch and latest commit:

```powershell
git branch --show-current
git log --oneline -3
```

Expected branch:

```text
BinanceTest
```

## Required Local Hotfixes

The `BinanceTest` branch may still contain two old/wrong details.

### 1. `/start` Title

In `bot/main.py`, the `/start` title should say:

```text
Krabs3 — Binance Futures Bot
```

not:

```text
Krabs3 — MEXC Futures Bot
```

### 2. Binance Demo Trading

In `bot/exchange/binance_client.py`, test/demo mode must use:

```python
self._exchange.enable_demo_trading(True)
```

not:

```python
self._exchange.set_sandbox_mode(True)
```

Reason: Binance deprecated the old futures sandbox/testnet flow. CCXT now expects Demo Trading.

These hotfixes should eventually be committed/pushed to `BinanceTest`. Until
then, every deploy agent must preserve or reapply them after switching branches.

### 3. Signal Image/Text Recognition And Execution

The bot can accept a trading signal as text, image caption, or screenshot.
Text/captions are parsed locally. Screenshots are sent to the configured
vision model, which must return strict JSON variables. The model decodes the
signal; it must not do market analysis and must not open trades by itself.

Expected input shape:

```text
EPIC USDT
SHORT
Entry 0.2098 / 0.2104
SL 0.2167
TP1 0.1978 TP2 0.1942 TP3 0.1903
3x
Confidence 96%
```

Expected bot behavior:

```text
1. Parse symbol, LONG/SHORT, entry range, SL, TP1/TP2/TP3, leverage, confidence.
2. Build algorithm variables for position/order execution:
   symbol, side, order_side, close_side, entry_min, entry_max, leverage,
   take_profit_orders[], stop_loss_order.
3. Resolve the raw symbol against the currently selected exchange client.
   Do not hardcode Binance or MEXC; use the active client's markets/contracts.
4. Show a confirmation card.
5. Wait for an inline button amount: $1, $2, $5, or $10.
6. Open the futures position only after confirmation.
7. Place 3 Binance reduce-only TAKE_PROFIT_MARKET orders.
8. Place 1 close-position STOP_MARKET SL order.
```

Default TP split when the signal does not specify shares:

```text
TP1 50%
TP2 25%
TP3 25%
```

Relevant files:

```text
bot/signals/model.py
bot/signals/parser.py
bot/signals/preview.py
bot/signals/store.py
bot/signals/execution.py
bot/signals/symbols.py
bot/signals/vision.py
bot/handlers/signals.py
bot/handlers/assistant.py
bot/exchange/binance_client.py
requirements.txt
```

Vision model config:

```text
/setkey openrouter_api_key YOUR_KEY
/setkey signal_vision_model openai/gpt-5.5
```

If `openai/gpt-5.5` is not available in the configured provider, change only
`signal_vision_model`; do not rewrite the signal execution algorithm.

Screenshot UX requirements:

```text
1. Set a Telegram reaction on the incoming signal when possible.
2. Send Telegram "typing..." action while decoding.
3. If the vision model/API protection/validation is unsure, reply with a
   warning emoji and explanation.
4. Do not silently swallow a signal message.
5. Do not block text/caption signals when screenshot decoding fails.
```

Do not use OCR for screenshots unless the user explicitly asks to replace the
vision model. The current requirement is vision-model decoding.

## Deploy To Toronto

Connect to the server. If SSH key auth works:

```powershell
ssh -i "O:\vpn-mur\vultr_key" root@155.138.140.211
```

If key auth fails, use password SSH with the credentials from:

```text
O:\vpn-mur\toronto.txt
```

On the server:

```bash
systemctl stop krabs.service

cd /root/krabs
git fetch origin
git switch -C BinanceTest origin/BinanceTest
```

Warning: the command above may overwrite local hotfixes depending on the current
working tree. After switching, verify and re-upload:

```text
bot/main.py
bot/exchange/binance_client.py
```

Then upload/copy the local hotfixed files to the server:

```text
K:\krabs-final\bot\main.py
K:\krabs-final\bot\exchange\binance_client.py
K:\krabs-final\bot\handlers\assistant.py
K:\krabs-final\bot\handlers\signals.py
K:\krabs-final\bot\signals\
K:\krabs-final\requirements.txt
K:\krabs-final\tests\test_signal_*.py
```

Remote destinations:

```text
/root/krabs/bot/main.py
/root/krabs/bot/exchange/binance_client.py
/root/krabs/bot/handlers/assistant.py
/root/krabs/bot/handlers/signals.py
/root/krabs/bot/signals/
/root/krabs/requirements.txt
/root/krabs/tests/
```

Install/update dependencies:

```bash
cd /root/krabs
.venv/bin/pip install -r requirements.txt
```

Restart:

```bash
systemctl restart krabs.service
systemctl enable krabs.service
```

Keep old bot stopped:

```bash
systemctl stop krabs4.service || true
systemctl disable krabs4.service || true
```

## Configure Telegram And Binance

Telegram token is stored in SQLite:

```text
/root/krabs/data/bot.db
```

### Demo Account Keys

For Binance Demo Trading, create keys here:

```text
https://demo.binance.com/en/my/settings/api-management
```

Then configure the bot in Telegram:

```text
/setkey exchange_provider binance_testnet
/setkey binance_api_key YOUR_DEMO_API_KEY
/setkey binance_secret YOUR_DEMO_SECRET
```

This is the safe test mode. It uses Binance Demo Trading through CCXT
`enable_demo_trading(True)`.

### Real Account Keys

For real Binance trading, create keys here:

```text
https://www.binance.com/en/my/settings/api-management
```

Then configure the bot in Telegram:

```text
/setkey exchange_provider binance
/setkey binance_api_key YOUR_REAL_API_KEY
/setkey binance_secret YOUR_REAL_SECRET
```

Use real Binance only intentionally. `binance` is live money.

Current expected safe provider:

```text
exchange_provider=binance_testnet
```

Important: despite the name `binance_testnet`, this must use Binance **Demo
Trading** keys now. Old Binance Futures Testnet keys from
`testnet.binancefuture.com` will fail.

## Verification

On Toronto:

```bash
cd /root/krabs
git branch --show-current
git rev-parse --short HEAD
grep -n "Krabs3" bot/main.py
grep -n "enable_demo_trading\|set_sandbox_mode" bot/exchange/binance_client.py
grep -n "set_multi_tp_sl" bot/exchange/binance_client.py
systemctl is-active krabs.service
systemctl is-active krabs4.service || true
ps -eo pid,ppid,lstart,cmd | grep -E "/root/krabs|/opt/krabs|start.py" | grep -v grep || true
```

Before changing Binance code or deployment settings, verify the plan/code
against these references:

```text
https://developers.binance.com/docs/derivatives/
https://demo-fapi.binance.com/
https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info
```

Expected:

```text
BinanceTest
krabs.service active
krabs4.service inactive
bot/main.py contains "Krabs3 — Binance Futures Bot"
bot/exchange/binance_client.py contains enable_demo_trading(True)
bot/exchange/binance_client.py contains set_multi_tp_sl
no set_sandbox_mode(True)
only one /root/krabs/start.py bot process
```

Verify signal parser and Binance multi-TP tests:

```bash
cd /root/krabs
.venv/bin/python -B -m unittest \
  tests.test_signal_parser \
  tests.test_signal_store \
  tests.test_signal_handler_format \
  tests.test_signal_execution \
  tests.test_signal_vision_decode \
  tests.test_signal_symbol_resolution
```

Expected:

```text
Ran 13 tests
OK
```

Also verify the provider stored in SQLite:

```bash
cd /root/krabs
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("data/bot.db")
for key in ("exchange_provider", "binance_api_key", "binance_secret"):
    row = conn.execute("select value from config where key=?", (key,)).fetchone()
    value = row[0] if row else ""
    if "secret" in key or "api_key" in key:
        value = "SET" if value else "MISSING"
    print(f"{key}={value or 'unset'}")
conn.close()
PY
```

Expected for demo mode:

```text
exchange_provider=binance_testnet
binance_api_key=SET
binance_secret=SET
```

Verify Telegram token without printing it:

```bash
cd /root/krabs
python3 - <<'PY'
import hashlib
import json
import sqlite3
import urllib.request

conn = sqlite3.connect("data/bot.db")
token = conn.execute(
    "select value from config where key=?",
    ("telegram_token",),
).fetchone()[0]
conn.close()

print("DB_TOKEN_SHA256=" + hashlib.sha256(token.encode()).hexdigest())
with urllib.request.urlopen(
    "https://api.telegram.org/bot" + token + "/getMe",
    timeout=15,
) as response:
    data = json.load(response)

print("GETME_OK=" + str(data.get("ok")))
print("BOT_USERNAME=" + str((data.get("result") or {}).get("username")))
PY
```

Expected:

```text
GETME_OK=True
BOT_USERNAME=krabsignalbot
```

Verify Binance client initialization:

```bash
cd /root/krabs
.venv/bin/python - <<'PY'
import sqlite3
from bot.exchange.binance_client import BinanceClient

conn = sqlite3.connect("data/bot.db")
get = lambda key: (conn.execute(
    "select value from config where key=?",
    (key,),
).fetchone() or [""])[0]

provider = get("exchange_provider") or "unset"
api_key = get("binance_api_key")
secret = get("binance_secret")
conn.close()

client = BinanceClient(api_key, secret, testnet=True)
print("PROVIDER=" + provider)
print("HAS_BINANCE_KEY=" + str(bool(api_key)))
print("INIT_OK=True")
print("HAS_ENABLE_DEMO=" + str(hasattr(client._exchange, "enable_demo_trading")))
PY
```

Expected:

```text
PROVIDER=binance_testnet
HAS_BINANCE_KEY=True
INIT_OK=True
HAS_ENABLE_DEMO=True
```

## Common Problems

### `binanceusdm testnet/sandbox mode is not supported`

Cause:

```text
bot/exchange/binance_client.py still calls set_sandbox_mode(True)
```

Fix:

```python
self._exchange.enable_demo_trading(True)
```

Restart after deploying the fixed file:

```bash
systemctl restart krabs.service
```

### Auth or Invalid API Key

Likely causes:

1. Old `testnet.binancefuture.com` keys were used.
2. Keys were created for live Binance while provider is `binance_testnet`.
3. Keys were created for Demo Trading but provider is `binance`.
4. API permissions are missing.

Use Demo Trading keys with:

```text
/setkey exchange_provider binance_testnet
```

Use live keys only with:

```text
/setkey exchange_provider binance
```

### Bot Shows MEXC In `/start`

Cause:

```text
bot/main.py was overwritten from origin/BinanceTest without the local title hotfix.
```

Fix the title and restart:

```text
Krabs3 — Binance Futures Bot
```

```bash
systemctl restart krabs.service
```

## Mistakes Agents Must Not Make

1. Do not deploy `main` when the user asks for Binance.
   - `main` is MEXC-only.
   - Use `origin/BinanceTest`.

2. Do not trust the branch name alone.
   - Verify `/root/krabs/bot/exchange/binance_client.py` contains
     `enable_demo_trading(True)`.
   - Verify it does not contain active `set_sandbox_mode(True)`.

3. Do not use old Binance Futures Testnet keys.
   - Use Binance Demo Trading API keys.
   - Demo keys and old testnet keys are not interchangeable.

4. Do not print raw Telegram token or Binance secrets.
   - Compare tokens by SHA-256 if needed.
   - Be careful: Telegram API URLs like `/bot<TOKEN>/getUpdates` include the
     token.

5. Do not leave two Krabs bots running.
   - `krabs.service` should be active.
   - `krabs4.service` should be inactive.

6. Do not delete or overwrite `/root/krabs/data/bot.db`.
   - It contains the Telegram token and exchange config.
   - Preserve it across code deploys.

7. Do not run destructive Git commands on the server without checking local
   hotfixes.
   - `git reset --hard` will remove the local `/start` title and Demo Trading
     hotfix unless they have been committed/pushed first.

8. Do not assume `binance_testnet` means the old Binance Futures Testnet.
   - In this codebase it should mean CCXT Binance Demo Trading mode.

9. Do not call `pip install` as proof the app is healthy.
   - Verify service status, process list, Telegram `getMe`, and Binance client
     initialization.

10. Do not switch to `exchange_provider=binance` unless the user explicitly wants
    live-money Binance trading.

11. Do not route signal screenshots through OCR by default.
    - Use the configured vision model to return strict JSON variables.
    - Use local validation before any order action.
    - Always show the confirmation card before opening a position.

12. Do not place one close-all TP for parsed 3TP signals.
    - Use partial reduce-only TP orders for TP1/TP2/TP3.
    - Keep one SL close-position order for the whole remaining position.

## Safety Notes

- Do not store raw Telegram tokens, Binance secrets, or VPS passwords in this file.
- Do not paste Telegram `getUpdates` URLs into chats or logs; those URLs include the bot token.
- Prefer Binance Demo Trading first.
- Treat `exchange_provider=binance` as real-money mode.
- Always verify there is only one running Krabs bot process after deploy.
