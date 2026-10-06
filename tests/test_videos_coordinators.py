from dataclasses import replace
from io import BytesIO
import re
import zipfile
import xml.etree.ElementTree as ET

import pytest

from smj.db import connection, initialize, upsert_coordinators
from smj.web import create_app
from test_sync import run


def csrf(client):
    html = client.get("/login", base_url="https://localhost").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if match:
        return match[1]
    with client.session_transaction(base_url="https://localhost") as session:
        return session["csrf"]


def login(client, password):
    return client.post("/login", base_url="https://localhost",
                       data={"csrf_token": csrf(client), "password": password})


def add_coordinator(settings, **overrides):
    initialize(settings.db_path)
    values = {"city": "Москва", "name": "Мария Координатор", "phone": "+7 900 000-00-00",
              "personal_email": "personal@example.test", "corporate_email": "moscow@smart-j.org",
              "birth_date": "1988-04-10"}
    values.update(overrides)
    with connection(settings.db_path) as db:
        upsert_coordinators(db, [values])
        return db.execute("SELECT id FROM coordinators WHERE city=? AND name=?",
                          [values["city"], values["name"]]).fetchone()[0]


@pytest.mark.parametrize("path", ["/tutors", "/videos", "/videos/new", "/coordinators",
    "/coordinators/new", "/videos/1/report.xlsx"])
def test_video_and_coordinator_pages_require_auth(settings, path):
    assert create_app(settings).test_client().get(path, base_url="https://localhost").status_code == 302


def test_coordinator_import_upserts_without_duplicates(settings):
    rows = [{"city": "Москва", "name": "Мария", "phone": "123"},
            {"city": "Минск", "name": "Анна", "corporate_email": "a@example.test\nb@example.test"}]
    initialize(settings.db_path)
    with connection(settings.db_path) as db:
        assert upsert_coordinators(db, rows) == 2
        assert upsert_coordinators(db, [{**rows[0], "phone": "456"}]) == 1
        assert db.execute("SELECT COUNT(*) FROM coordinators").fetchone()[0] == 2
        assert db.execute("SELECT phone FROM coordinators WHERE city='Москва'").fetchone()[0] == "456"
        assert db.execute("SELECT corporate_email FROM coordinators WHERE city='Минск'").fetchone()[0] == \
            "a@example.test\nb@example.test"
        assert db.execute("SELECT birth_date FROM coordinators WHERE city='Минск'").fetchone()[0] == ""
        with pytest.raises(ValueError):
            upsert_coordinators(db, [{"city": "", "name": "Missing city"}])


def test_coordinator_edit_form_saves_contacts(settings):
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    edit_url = f"/coordinators/{coordinator_id}/edit"
    page = client.get(edit_url, base_url="https://localhost")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))[1]
    response = client.post(edit_url, base_url="https://localhost", data={
        "csrf_token": csrf_token, "city": "Москва", "name": "Мария Координатор",
        "phone": "+7 911 111-11-11", "personal_email": "new@example.test",
        "corporate_email": "one@smart-j.org\ntwo@smart-j.org",
    })
    assert response.status_code == 302
    listing = client.get("/coordinators", base_url="https://localhost").get_data(as_text=True)
    assert "+7 911 111-11-11" in listing and "two@smart-j.org" in listing


def test_teacher_report_links_modules_and_counts_videos(settings, lesson):
    lessons = [lesson, replace(lesson, module="Matata", topic="Тема 2")]
    run(settings, lessons)
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    for module in ("Kids", "Matata"):
        response = client.post("/videos/new", base_url="https://localhost", data={
            "csrf_token": csrf(client), "city": lesson.city, "teacher": lesson.teacher,
            "request_date": "2026-10-05", "sent_date": "2026-10-06", "module": module,
            "video_url": "https://video.example.test/lesson", "coordinator_id": str(coordinator_id),
            "positive_notes": "Хороший темп", "growth_notes": "Добавить вопросы",
        })
        assert response.status_code == 302
    page = client.get("/tutors", base_url="https://localhost").get_data(as_text=True)
    assert "Учитель 1" in page and ">2</a>" in page
    assert "/kids?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0&amp;teacher=" in page
    assert "/matata?city=" in page
    assert client.get("/videos?city=Москва&teacher=Учитель 1",
                      base_url="https://localhost").get_data(as_text=True).count("Скачать Excel") == 2
    assert client.get("/kids?city=Москва&teacher=Учитель 1",
                      base_url="https://localhost").status_code == 200


def test_video_excel_contains_reference_fields_and_safe_text(settings):
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    response = client.post("/videos/new", base_url="https://localhost", data={
        "csrf_token": csrf(client), "city": "Москва", "teacher": "Елена Пример",
        "request_date": "2026-10-05", "sent_date": "2026-10-06", "module": "Kids",
        "video_url": "https://rutube.ru/video/private/test", "coordinator_id": str(coordinator_id),
        "positive_notes": "=1+1 как обычный текст", "growth_notes": "Уточнить темп занятия",
    })
    assert response.status_code == 302
    report = client.get("/videos/1/report.xlsx", base_url="https://localhost")
    assert report.status_code == 200
    assert report.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert ".xlsx" in report.headers["Content-Disposition"]
    with zipfile.ZipFile(BytesIO(report.data)) as archive:
        shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        strings = ["".join(item.itertext()) for item in shared]
        assert "Филиал" in strings and "Дата отправки" in strings
        assert "Что хорошо:" in strings and "Зона роста:" in strings
        assert "=1+1 как обычный текст" in strings
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        assert sheet.find(".//{*}hyperlinks") is not None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd"])
def test_video_rejects_non_web_links(settings, url):
    client = create_app(settings).test_client()
    login(client, settings.password)
    response = client.post("/videos/new", base_url="https://localhost", data={
        "csrf_token": csrf(client), "city": "Москва", "teacher": "Елена",
        "request_date": "2026-10-05", "sent_date": "2026-10-06", "module": "Kids",
        "video_url": url, "coordinator_id": "", "positive_notes": "", "growth_notes": "",
    })
    assert response.status_code == 400
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
