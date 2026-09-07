"""Quick setup and launch script."""
import argparse
import os
import sys
from pathlib import Path
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

PID_FILE = Path(os.environ.get("KRABS_DATA_DIR", Path(__file__).parent / "data")) / "bot.pid"


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"service": "krabs", "mode": os.environ.get("KRABS_RUN_MODE", "standby"),
                           "revision": os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("KRABS_REVISION", "local")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_standby():
    from bot import db
    db.init_db()
    print("Krabs standby: health endpoint only; Telegram polling and trading are disabled.", flush=True)
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), HealthHandler).serve_forever()


_pid_handle = None


def _acquire_pid_lock():
    global _pid_handle
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = PID_FILE.open("a+")
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            if not PID_FILE.stat().st_size:
                handle.write("0")
                handle.flush()
                handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another bot process holds the data-directory lock")
    _pid_handle = handle


def _release_pid_lock():
    global _pid_handle
    if _pid_handle:
        _pid_handle.close()
        _pid_handle = None


def setup():
    from bot import db as db_mod
    db_mod.init_db()
    print("=== Krabs3 Setup ===")
    token = input("Telegram bot token: ").strip()
    db_mod.set_config("telegram_token", token)

    user_ids = input("Allowed Telegram user IDs (comma-separated): ").strip()
    db_mod.set_config("allowed_user_ids", user_ids)

    mexc_key = input("MEXC API key: ").strip()
    db_mod.set_config("mexc_api_key", mexc_key)

    mexc_secret = input("MEXC API secret: ").strip()
    db_mod.set_config("mexc_secret", mexc_secret)

    or_key = input("OpenRouter API key (or leave blank): ").strip()
    if or_key:
        db_mod.set_config("openrouter_api_key", or_key)

    print("\nSetup complete. Run: python start.py")


def run():
    mode = os.environ.get("KRABS_RUN_MODE", "standby")
    if mode == "standby":
        serve_standby()
        return
    if mode != "bot":
        raise ValueError("KRABS_RUN_MODE must be standby or bot")
    _acquire_pid_lock()
    try:
        from threading import Thread
        server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), HealthHandler)
        Thread(target=server.serve_forever, daemon=True).start()
        from bot.main import main
        main()
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true", help="Run interactive setup")
    args = parser.parse_args()
    if args.setup:
        setup()
    else:
        run()
