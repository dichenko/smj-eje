from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


MODULES = {"Matata": 33, "Kids": 55, "UserBasic": 59, "Junior": 56}
ROOT = Path(__file__).resolve().parent.parent


def flag(name, default=False):
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Settings:
    db_path: Path
    backup_dir: Path
    password: str
    secret_key: str
    timezone: ZoneInfo
    start_date: date
    mutable_days: int
    session_days: int
    stale_minutes: int
    production: bool
    source_url: str
    source_username: str
    source_password: str
    report_prefix: str
    allow_http: bool
    request_pause: float
    max_removal_ratio: float

    @classmethod
    def from_env(cls):
        load_dotenv(Path(os.getenv("ENV_FILE", ROOT / ".env")), override=False)
        env = os.getenv("APP_ENV", "production")
        if env not in {"production", "development"}:
            raise ValueError("APP_ENV must be production or development")
        settings = cls(
            db_path=Path(os.getenv("DB_PATH", ROOT / "data/reports.db")).resolve(),
            backup_dir=Path(os.getenv("BACKUP_DIR", ROOT / "backups")).resolve(),
            password=os.getenv("APP_PASSWORD", ""),
            secret_key=os.getenv("FLASK_SECRET_KEY", ""),
            timezone=ZoneInfo(os.getenv("TZ", "Europe/Moscow")),
            start_date=date.fromisoformat(os.getenv("DATA_START_DATE", "2026-09-01")),
            mutable_days=int(os.getenv("SYNC_MUTABLE_DAYS", "7")),
            session_days=int(os.getenv("SESSION_DAYS", "7")),
            stale_minutes=int(os.getenv("STALE_AFTER_MINUTES", "120")),
            production=env == "production",
            source_url=os.getenv("SMARTJ_BASE_URL", "https://my.smart-j.ru").rstrip("/"),
            source_username=os.getenv("SMARTJ_USERNAME", ""),
            source_password=os.getenv("SMARTJ_PASSWORD", ""),
            report_prefix=os.getenv("SMARTJ_REPORT_PREFIX", "/r1869~plan/kt-plan-report").rstrip("/"),
            allow_http=flag("SMARTJ_ALLOW_HTTP"),
            request_pause=float(os.getenv("SMARTJ_REQUEST_PAUSE", "2")),
            max_removal_ratio=float(os.getenv("SYNC_MAX_REMOVAL_RATIO", "0.5")),
        )
        if min(settings.mutable_days, settings.session_days, settings.stale_minutes) < 1:
            raise ValueError("Day/minute limits must be positive")
        if not 0 <= settings.max_removal_ratio <= 1 or settings.request_pause < 0:
            raise ValueError("Invalid sync limits")
        parsed = urlsplit(settings.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ValueError("SMARTJ_BASE_URL must be an HTTP(S) URL without credentials")
        if parsed.scheme == "http" and not settings.allow_http:
            raise ValueError("HTTP source requires explicit SMARTJ_ALLOW_HTTP=true")
        return settings

    def validate_web(self):
        if len(self.password) < 32 or len(self.secret_key) < 32:
            raise ValueError("APP_PASSWORD and FLASK_SECRET_KEY must each have at least 32 characters")

    def validate_source(self):
        if not self.source_username or not self.source_password:
            raise ValueError("SMARTJ_USERNAME and SMARTJ_PASSWORD are required for synchronization")
