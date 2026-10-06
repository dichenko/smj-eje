from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3


@contextmanager
def connection(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with connection(path) as db:
        # Small, single-user database: rollback journal avoids requiring a patched WAL runtime.
        db.execute("PRAGMA journal_mode=DELETE")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS lessons (
                source_key TEXT PRIMARY KEY,
                stable_id TEXT,
                module TEXT NOT NULL,
                topic TEXT NOT NULL,
                city TEXT NOT NULL,
                teacher TEXT NOT NULL,
                group_name TEXT NOT NULL,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS lessons_date ON lessons(date);
            CREATE INDEX IF NOT EXISTS lessons_city ON lessons(city, module, date);
            CREATE INDEX IF NOT EXISTS lessons_teacher ON lessons(teacher, date);
            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                error TEXT,
                counts TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip TEXT NOT NULL,
                attempted_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS login_attempt_time ON login_attempts(attempted_at);
            CREATE TABLE IF NOT EXISTS coordinators (
                id INTEGER PRIMARY KEY,
                city TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                personal_email TEXT NOT NULL DEFAULT '',
                corporate_email TEXT NOT NULL DEFAULT '',
                birth_date TEXT NOT NULL DEFAULT '',
                telegram_username TEXT NOT NULL DEFAULT '',
                UNIQUE(city, name)
            );
            CREATE INDEX IF NOT EXISTS coordinators_city ON coordinators(city, name);
            CREATE TABLE IF NOT EXISTS tutor_contacts (
                city TEXT NOT NULL,
                teacher TEXT NOT NULL,
                telegram_username TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(city, teacher)
            );
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY,
                city TEXT NOT NULL,
                teacher TEXT NOT NULL,
                coordinator_id INTEGER,
                request_date TEXT NOT NULL,
                sent_date TEXT,
                module TEXT NOT NULL,
                video_url TEXT NOT NULL DEFAULT '',
                positive_notes TEXT NOT NULL DEFAULT '',
                growth_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS videos_teacher ON videos(city, teacher, request_date);
        """)
        # Existing VPS databases were created before Telegram fields existed.
        coordinator_columns = {row["name"] for row in db.execute("PRAGMA table_info(coordinators)")}
        if "telegram_username" not in coordinator_columns:
            db.execute("ALTER TABLE coordinators ADD COLUMN telegram_username TEXT NOT NULL DEFAULT ''")


def upsert_coordinators(db, rows):
    """Import coordinator contact rows without replacing unrelated database records."""
    imported = 0
    for row in rows:
        city = str(row.get("city", "")).strip()
        name = str(row.get("name", "")).strip()
        if not city or not name:
            raise ValueError("Every coordinator requires a city and name")
        db.execute("""
            INSERT INTO coordinators(city,name,phone,personal_email,corporate_email,birth_date)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(city,name) DO UPDATE SET
                phone=excluded.phone,
                personal_email=excluded.personal_email,
                corporate_email=excluded.corporate_email,
                birth_date=excluded.birth_date
        """, [city, name, str(row.get("phone", "")).strip(),
              str(row.get("personal_email", "")).strip(),
              str(row.get("corporate_email", "")).strip(),
              str(row.get("birth_date", "")).strip()])
        imported += 1
    return imported


def utc_now():
    return datetime.now(timezone.utc)


def sync_status(settings):
    with connection(settings.db_path) as db:
        attempt = db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        success = db.execute(
            "SELECT * FROM sync_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        total = db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    last = datetime.fromisoformat(success["finished_at"]) if success else None
    return {
        "last_attempt": dict(attempt) if attempt else None,
        "last_success": last.astimezone(settings.timezone).strftime("%d.%m.%Y %H:%M") if last else None,
        "stale": last is None or utc_now() - last > timedelta(minutes=settings.stale_minutes),
        "total": total,
        "counts": json.loads(success["counts"]) if success else {},
    }


def distinct_values(db, field, city=None):
    if field not in {"city", "teacher", "group_name"}:
        raise ValueError("Unsupported field")
    where, params = (" WHERE city=?", [city]) if city else ("", [])
    return [r[0] for r in db.execute(f"SELECT DISTINCT {field} FROM lessons{where} ORDER BY {field}", params)]


def query_lessons(db, filters, page=1, per_page=25):
    clauses, params = [], []
    for field in ("module", "city", "teacher", "group_name"):
        if filters.get(field):
            clauses.append(f"{field}=?")
            params.append(filters[field])
    for field, op in (("start", ">="), ("end", "<=")):
        if filters.get(field):
            clauses.append(f"date {op} ?")
            params.append(filters[field])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = db.execute("SELECT COUNT(*) FROM lessons" + where, params).fetchone()[0]
    rows = db.execute(
        "SELECT module,topic,city,teacher,group_name,date FROM lessons" + where +
        " ORDER BY date DESC,city,module,topic,group_name,source_key LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return [dict(r) for r in rows], total
