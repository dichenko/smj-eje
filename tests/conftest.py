from dataclasses import replace

import pytest

from smj.config import Settings
from smj.db import connection
from smj.source import Lesson


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("SMARTJ_BASE_URL", "https://example.test")
    return replace(Settings.from_env(), db_path=tmp_path / "reports.db",
                   backup_dir=tmp_path / "backups", password="A" * 48, secret_key="B" * 64,
                   source_username="test", source_password="test", request_pause=0)


@pytest.fixture
def lesson():
    return Lesson("Kids", "Тема 1", "Москва", "Учитель 1", "Группа 1", "2026-10-01", "1")


def rows(settings):
    with connection(settings.db_path) as db:
        return [dict(r) for r in db.execute("SELECT * FROM lessons ORDER BY source_key")]
