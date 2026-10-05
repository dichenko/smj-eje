from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
import json
import logging
import os

from .config import MODULES
from .db import connection, initialize, utc_now
from .source import SmartJSource, SourceError, window_start


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def sync_lock(path):
    """Kernel-owned lock: released even on kill/reboot; shared by CLI and systemd."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if os.name == "nt":
            import msvcrt
            if os.fstat(file.fileno()).st_size == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AlreadyRunning("Another synchronization is already running") from None
        else:
            import fcntl
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning("Another synchronization is already running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


def reconcile(db, snapshot, settings, today, accept_large_deletions=False):
    if set(snapshot) != set(MODULES):
        raise SourceError("Incomplete module snapshot; database unchanged")
    since = window_start(settings, today).isoformat()
    now = utc_now().isoformat()
    existing = {r["source_key"]: dict(r) for r in db.execute("SELECT * FROM lessons")}
    initial_load = db.execute("SELECT 1 FROM sync_runs WHERE status='success' LIMIT 1").fetchone() is None
    incoming, seen = {}, Counter()
    # Keep pre-period records with stable IDs for detecting date moves out of the allowed period.
    for module, lessons in snapshot.items():
        for lesson in lessons:
            if lesson.module != module:
                raise SourceError("Unexpected module in snapshot")
            parsed_date = date.fromisoformat(lesson.date)
            if parsed_date > today:
                raise SourceError("Source contains a future performed lesson; database unchanged")
            if lesson.stable_id:
                key = f"id:{module}:{lesson.stable_id}"
                if key in incoming and incoming[key] != lesson:
                    raise SourceError("Conflicting source IDs; database unchanged")
            else:
                fingerprint = lesson.fingerprint()
                seen[fingerprint] += 1
                key = f"snapshot:{fingerprint}:{seen[fingerprint]}"
            incoming[key] = lesson

    changes = {"added": 0, "updated": 0, "deleted": 0, "frozen": 0,
               "ignored_archived": 0, "modules": {m: len(v) for m, v in snapshot.items()}}
    eligible = {k: r for k, r in existing.items() if since <= r["date"] <= today.isoformat()}
    missing = {k: r for k, r in eligible.items() if k not in incoming}
    # Protect each module independently, even when other modules are healthy.
    if not accept_large_deletions:
        for module in MODULES:
            count = sum(r["module"] == module for r in eligible.values())
            removed = sum(r["module"] == module for r in missing.values())
            if count >= 10 and removed / count > settings.max_removal_ratio:
                raise SourceError(f"{module}: suspicious change volume; inspect source, then use --accept-large-deletions")
    for key, lesson in incoming.items():
        fields = asdict(lesson)
        row = existing.get(key)
        if row and row["date"] < since:
            changes["frozen"] += 1
            continue
        if lesson.date < settings.start_date.isoformat():
            if row:
                db.execute("DELETE FROM lessons WHERE source_key=?", [key])
                changes["deleted"] += 1
            continue
        if not row:
            if not initial_load and lesson.date < since:
                changes["ignored_archived"] += 1
                continue
            db.execute("""INSERT INTO lessons
                (source_key,stable_id,module,topic,city,teacher,group_name,date,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", [key, lesson.stable_id, lesson.module, lesson.topic,
                lesson.city, lesson.teacher, lesson.group_name, lesson.date, now, now])
            changes["added"] += 1
        elif any(row[field] != fields[field] for field in fields):
            db.execute("""UPDATE lessons SET stable_id=?,module=?,topic=?,city=?,teacher=?,
                group_name=?,date=?,updated_at=? WHERE source_key=?""", [lesson.stable_id,
                lesson.module, lesson.topic, lesson.city, lesson.teacher, lesson.group_name,
                lesson.date, now, key])
            changes["updated"] += 1
    for key in missing:
        db.execute("DELETE FROM lessons WHERE source_key=?", [key])
        changes["deleted"] += 1
    return changes


def synchronize(settings, source=None, today=None, accept_large_deletions=False):
    initialize(settings.db_path)
    with sync_lock(settings.db_path.with_suffix(".sync.lock")):
        started = utc_now()
        today = today or started.astimezone(settings.timezone).date()
        with connection(settings.db_path) as db:
            # Previous kernel lock is gone, so any unfinished run was interrupted.
            db.execute("UPDATE sync_runs SET status='interrupted',finished_at=?,error=? WHERE status='running'",
                       [started.isoformat(), "Synchronization was interrupted", ])
            run_id = db.execute("INSERT INTO sync_runs(started_at,status) VALUES (?, 'running')",
                                [started.isoformat()]).lastrowid
        owned_source = source is None
        source = source or SmartJSource(settings)
        try:
            snapshot = source.collect()
            with connection(settings.db_path) as db:
                counts = reconcile(db, snapshot, settings, today, accept_large_deletions)
                counts["duration_seconds"] = round((utc_now() - started).total_seconds(), 2)
                db.execute("UPDATE sync_runs SET status='success',finished_at=?,counts=? WHERE id=?",
                           [utc_now().isoformat(), json.dumps(counts), run_id])
            logging.info("Synchronization complete: added=%s updated=%s removed=%s ignored_archived=%s",
                         counts["added"], counts["updated"], counts["deleted"], counts["ignored_archived"])
            return counts
        except Exception as error:
            # Only known sanitized errors may be shown on the website.
            message = str(error) if isinstance(error, SourceError) else "Internal synchronization error; see service status"
            with connection(settings.db_path) as db:
                db.execute("UPDATE sync_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                           [utc_now().isoformat(), message, run_id])
            logging.error("Synchronization failed: %s (%s)", message, type(error).__name__)
            raise
        finally:
            if owned_source:
                source.close()
