import sqlite3

from smj.cli import backup
from test_sync import run


def test_consistent_backup_and_restore(settings, lesson):
    run(settings, [lesson])
    destination = backup(settings)
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT teacher FROM lessons").fetchone()[0] == lesson.teacher
    finally:
        restored.close()
    assert list(settings.backup_dir.rglob("*.tmp")) == []


def test_backup_retention(settings, lesson):
    run(settings, [lesson])
    for _ in range(9):
        backup(settings)
    assert len(list((settings.backup_dir / "daily").glob("*.db"))) == 7
