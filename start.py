"""Quick setup and launch script."""
import argparse
import os
import sys
from pathlib import Path

PID_FILE = Path(__file__).parent / "data" / "bot.pid"


def _acquire_pid_lock():
    """Exit if another instance is already running."""
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text().strip())
            # Check if that process is actually alive
            os.kill(existing_pid, 0)
            print(f"ERROR: bot already running (PID {existing_pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, PermissionError):
            # Stale PID file — process is dead
            PID_FILE.unlink(missing_ok=True)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _release_pid_lock():
    PID_FILE.unlink(missing_ok=True)


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
    _acquire_pid_lock()
    try:
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
