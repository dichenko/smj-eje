import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
import secrets
import shutil
import sqlite3

from .config import Settings
from .db import connection, initialize
from .sync import AlreadyRunning, synchronize


def backup(settings):
    if not settings.db_path.exists():
        raise ValueError("Database does not exist; run smj init-db first")
    stamp = datetime.now(settings.timezone)
    directory = settings.backup_dir / "daily"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"reports-{stamp.strftime('%Y%m%d-%H%M%S-%f')}.db"
    temporary = destination.with_suffix(".tmp")
    try:
        with connection(settings.db_path) as source:
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
            finally:
                target.close()
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        if stamp.weekday() == 6:
            weekly = settings.backup_dir / "weekly"
            weekly.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, weekly / f"reports-{stamp.strftime('%Y-%W')}.db")
        for bucket, keep in ((directory, 7), (settings.backup_dir / "weekly", 4)):
            for old in sorted(bucket.glob("reports-*.db"), reverse=True)[keep:]:
                old.unlink()
        logging.info("Backup saved: %s", destination.name)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Smart-J reports")
    parser.add_argument("--env-file", type=Path, help="Explicit path to .env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Initialize an empty database")
    sync_parser = commands.add_parser("sync", help="Synchronize immediately (no timer delay)")
    sync_parser.add_argument("--accept-large-deletions", action="store_true",
                             help="Accept a large change after manually verifying the source")
    commands.add_parser("backup", help="Create a consistent SQLite backup")
    commands.add_parser("secrets", help="Generate new APP_PASSWORD and FLASK_SECRET_KEY")
    serve = commands.add_parser("serve", help="Local development server only")
    serve.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "secrets":
        print(f"APP_PASSWORD={secrets.token_urlsafe(36)}")
        print(f"FLASK_SECRET_KEY={secrets.token_hex(32)}")
        return 0
    if args.env_file:
        os.environ["ENV_FILE"] = str(args.env_file.resolve())
    try:
        settings = Settings.from_env()
        if args.command == "init-db":
            initialize(settings.db_path)
            logging.info("Database initialized")
        elif args.command == "sync":
            synchronize(settings, accept_large_deletions=args.accept_large_deletions)
        elif args.command == "backup":
            backup(settings)
        elif args.command == "serve":
            if settings.production:
                raise ValueError("Use Gunicorn in production; set APP_ENV=development for local preview")
            from .web import create_app
            create_app(settings).run(host="127.0.0.1", port=args.port, debug=False)
        return 0
    except AlreadyRunning:
        logging.error("Synchronization already running")
        return 2
    except Exception as error:
        # Do not leak credentials or third-party response bodies in a terminal/journal.
        if isinstance(error, ValueError):
            logging.error("%s", error)
        else:
            logging.error("Command failed (%s). Check configuration and synchronization status.",
                          type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
