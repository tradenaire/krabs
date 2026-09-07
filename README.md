# Krabs — MEXC Futures Telegram bot

Current main contains the code deployed to Railway `t3-remote / krabs` from local revision `a708898`: position ownership and TP/SL safety fixes, closure/PnL accounting, MEXC asset balances, compact balance messages, parallel balance requests, and Telegram typing feedback.

## Deployment

Build with the included Dockerfile. It runs the offline tests before starting.
Set these private environment variables in Railway; never commit their values:

- `TELEGRAM_TOKEN`
- `ALLOWED_USER_IDS` (comma-separated numeric Telegram user IDs)
- `MEXC_API_KEY`
- `MEXC_SECRET`
- `EXCHANGE_PROVIDER=mexc`
- `KRABS_RUN_MODE=bot` (the container defaults to `standby`)
- `KRABS_DATA_DIR=/data` (mount a persistent volume here)
- `AUTO_SCAN_ENABLED=false` (current deployment setting)

Railway `t3-remote / krabs` builds `tradenaire/krabs`, branch `main`. `.github/workflows/deploy-railway.yml` triggers a deployment of the exact pushed commit and waits for Railway SUCCESS; it also supports manual dispatch on main. It uses the `RAILWAY_KRABS_TOKEN` GitHub Actions secret, scoped to `t3-remote / production`. Docker runs the tests before starting the bot. Do not upload a local folder with `railway up`.

## Validation and limits

Run `python -m unittest discover -s tests -q` with `NUMBA_DISABLE_JIT=1`. The deployed revision passed 60 tests.

Manual exchange positions require explicit `/adopt` before the scheduler manages them. Multi-asset balances use market-price estimates and account debt. Available cross-margin for opening trades is not confirmed by the current API integration and is shown as unavailable. Futures collateral is displayed separately from free margin.

The other Markdown reports and result JSON files contain historical verification snapshots. Their older revision and deployment IDs are not the current deployment.
