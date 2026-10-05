from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from smj.config import MODULES
from smj.db import connection
from smj.source import SourceError
from smj.sync import AlreadyRunning, synchronize, sync_lock
from conftest import rows


TODAY = date(2026, 10, 5)


class Source:
    def __init__(self, lessons):
        self.snapshot = {m: [] for m in MODULES}
        for lesson in lessons:
            self.snapshot[lesson.module].append(lesson)

    def collect(self):
        return self.snapshot


def run(settings, lessons, today=TODAY, **kwargs):
    return synchronize(settings, Source(lessons), today=today, **kwargs)


def test_initial_period_and_idempotence(settings, lesson):
    source = [replace(lesson, date="2026-08-31", stable_id="old"),
              replace(lesson, date="2026-09-01", stable_id="boundary"), lesson]
    assert run(settings, source)["added"] == 2
    assert run(settings, source)["added"] == 0
    assert {r["date"] for r in rows(settings)} == {"2026-09-01", "2026-10-01"}


def test_recent_update_changes_all_fields_without_duplicate(settings, lesson):
    run(settings, [lesson])
    changed = replace(lesson, teacher="Другой учитель", group_name="Новая группа",
                      city="Минск", topic="Другая тема", date="2026-09-29")
    assert run(settings, [changed])["updated"] == 1
    saved = rows(settings)
    assert len(saved) == 1
    assert saved[0]["teacher"] == changed.teacher
    assert saved[0]["date"] == changed.date


def test_six_days_mutable_exact_seven_frozen(settings, lesson):
    young = replace(lesson, date="2026-09-29", stable_id="young")
    old = replace(lesson, date="2026-09-28", stable_id="old")
    run(settings, [young, old])
    run(settings, [replace(young, teacher="Новый"), replace(old, teacher="Новый")])
    saved = {r["stable_id"]: r for r in rows(settings)}
    assert saved["young"]["teacher"] == "Новый"
    assert saved["old"]["teacher"] == lesson.teacher


def test_deletions_only_recent(settings, lesson):
    old = replace(lesson, date="2026-09-28", stable_id="old")
    run(settings, [lesson, old])
    assert run(settings, [])["deleted"] == 1
    assert rows(settings)[0]["stable_id"] == "old"


def test_late_old_lesson_ignored_after_initial_load(settings, lesson):
    run(settings, [lesson])
    old = replace(lesson, date="2026-09-10", stable_id="late")
    assert run(settings, [lesson, old])["ignored_archived"] == 1
    assert len(rows(settings)) == 1


def test_date_move_from_young_to_old_is_applied_once(settings, lesson):
    run(settings, [lesson])
    moved = replace(lesson, date="2026-09-10")
    run(settings, [moved])
    run(settings, [replace(moved, teacher="Поздняя правка")])
    assert rows(settings)[0]["date"] == "2026-09-10"
    assert rows(settings)[0]["teacher"] == lesson.teacher


def test_date_move_outside_period_removes_recent_record(settings, lesson):
    run(settings, [lesson])
    run(settings, [replace(lesson, date="2026-08-31")])
    assert rows(settings) == []


def test_window_rolls_over_midnight(settings, lesson):
    item = replace(lesson, date="2026-09-28")
    run(settings, [item], today=date(2026, 10, 4))
    run(settings, [replace(item, teacher="Изменено")], today=TODAY)
    assert rows(settings)[0]["teacher"] == lesson.teacher


def test_moscow_not_utc_boundary(settings, lesson, monkeypatch):
    item = replace(lesson, date="2026-09-28")
    run(settings, [item], today=date(2026, 10, 4))
    monkeypatch.setattr("smj.sync.utc_now", lambda: datetime(2026, 10, 4, 21, 1, tzinfo=timezone.utc))
    synchronize(settings, Source([replace(item, teacher="Не применить")]))
    assert rows(settings)[0]["teacher"] == lesson.teacher


def test_missing_module_does_not_touch_database(settings, lesson):
    run(settings, [lesson])
    original = rows(settings)
    source = Source([])
    del source.snapshot["Junior"]
    with pytest.raises(SourceError, match="Incomplete"):
        synchronize(settings, source, today=TODAY)
    assert rows(settings) == original
    with connection(settings.db_path) as db:
        assert db.execute("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "failed"


def test_source_failure_keeps_last_success(settings, lesson):
    run(settings, [lesson])
    class Broken:
        def collect(self):
            raise SourceError("Missing table")
    with pytest.raises(SourceError):
        synchronize(settings, Broken(), today=TODAY)
    assert len(rows(settings)) == 1
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sync_runs WHERE status='success'").fetchone()[0] == 1


def test_database_failure_rolls_back_every_module(settings, lesson):
    run(settings, [lesson])
    with connection(settings.db_path) as db:
        db.execute("CREATE TRIGGER reject_insert BEFORE INSERT ON lessons BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(Exception):
        run(settings, [replace(lesson, teacher="Новый"), replace(lesson, stable_id="2")])
    assert rows(settings)[0]["teacher"] == lesson.teacher


def test_conflicting_ids_rejected(settings, lesson):
    with pytest.raises(SourceError, match="Conflicting"):
        run(settings, [lesson, replace(lesson, teacher="Другой")])
    assert rows(settings) == []


def test_two_groups_same_topic_city_date_remain_distinct(settings, lesson):
    first = replace(lesson, stable_id=None)
    second = replace(first, group_name="Группа 2")
    run(settings, [first, second])
    run(settings, [first, second])
    assert len(rows(settings)) == 2


def test_fallback_recent_snapshot_reconciles_all_fields(settings, lesson):
    first = replace(lesson, stable_id=None)
    run(settings, [first])
    changed = replace(first, teacher="Новый", date="2026-09-29", group_name="Другая")
    run(settings, [changed])
    saved = rows(settings)
    assert len(saved) == 1
    assert saved[0]["teacher"] == "Новый"
    assert saved[0]["group_name"] == "Другая"


def test_fallback_frozen_changes_do_not_duplicate_archive(settings, lesson):
    old = replace(lesson, stable_id=None, date="2026-09-01")
    run(settings, [old])
    result = run(settings, [replace(old, teacher="Новый", group_name="Другая")])
    assert result["ignored_archived"] == 1
    assert len(rows(settings)) == 1
    assert rows(settings)[0]["teacher"] == lesson.teacher


def test_identical_duplicates_are_counted_and_idempotent(settings, lesson):
    first = replace(lesson, stable_id=None)
    run(settings, [first, first])
    run(settings, [first, first])
    assert len(rows(settings)) == 2
    run(settings, [first])
    assert len(rows(settings)) == 1


def test_mass_deletion_guard_and_manual_override(settings, lesson):
    items = [replace(lesson, stable_id=str(i)) for i in range(12)]
    run(settings, items)
    with pytest.raises(SourceError, match="suspicious"):
        run(settings, [])
    assert len(rows(settings)) == 12
    run(settings, [], accept_large_deletions=True)
    assert rows(settings) == []


def test_lock_released_on_exception(settings):
    lock = settings.db_path.with_suffix(".sync.lock")
    with pytest.raises(ValueError):
        with sync_lock(lock):
            with pytest.raises(AlreadyRunning):
                with sync_lock(lock):
                    pass
            raise ValueError("test")
    with sync_lock(lock):
        pass


def test_rejected_future_data_never_committed(settings, lesson):
    with pytest.raises(SourceError, match="future"):
        run(settings, [replace(lesson, date="2026-10-06")])
    assert rows(settings) == []
