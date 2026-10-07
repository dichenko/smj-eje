from dataclasses import replace
from io import BytesIO
import re
from datetime import datetime
import sqlite3
import json
from urllib.parse import parse_qs, urlparse
import zipfile
import xml.etree.ElementTree as ET

import pytest
from bs4 import BeautifulSoup

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


@pytest.mark.parametrize("path", ["/tutors", "/tutors/profile", "/videos", "/videos/new", "/coordinators",
    "/coordinators/new", "/videos/1/report.xlsx", "/videos/1/delete"])
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


def test_coordinator_telegram_username_is_saved_and_linked(settings):
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    edit_url = f"/coordinators/{coordinator_id}/edit"
    page = client.get(edit_url, base_url="https://localhost")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))[1]
    response = client.post(edit_url, base_url="https://localhost", data={
        "csrf_token": csrf_token, "city": "Москва", "name": "Мария Координатор",
        "telegram_username": "@maria_coord", "phone": "", "personal_email": "",
        "corporate_email": "", "birth_date": "",
    })
    assert response.status_code == 302
    listing = client.get("/coordinators", base_url="https://localhost").get_data(as_text=True)
    assert 'href="https://telegram.me/maria_coord"' in listing


def test_existing_coordinator_database_migrates_telegram_column(settings):
    with sqlite3.connect(settings.db_path) as db:
        db.execute("""CREATE TABLE coordinators (
            id INTEGER PRIMARY KEY, city TEXT NOT NULL, name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '', personal_email TEXT NOT NULL DEFAULT '',
            corporate_email TEXT NOT NULL DEFAULT '', birth_date TEXT NOT NULL DEFAULT '',
            UNIQUE(city,name))""")
        db.execute("INSERT INTO coordinators(city,name) VALUES('Москва','Мария')")
    initialize(settings.db_path)
    with connection(settings.db_path) as db:
        row = db.execute("SELECT name,telegram_username FROM coordinators").fetchone()
        assert tuple(row) == ("Мария", "")


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


def test_teacher_profile_shows_lessons_and_allows_telegram_edit(settings, lesson):
    run(settings, [lesson, replace(lesson, stable_id="2", topic="Тема 2", date="2026-10-02")])
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    video_response = client.post("/videos/new", base_url="https://localhost", data={
        "csrf_token": csrf(client), "city": "Москва", "teacher": "Учитель 1",
        "request_date": "2026-10-03", "sent_date": "2026-10-04", "module": "Kids",
        "video_url": "https://video.example.test/tutor", "coordinator_id": str(coordinator_id),
        "positive_notes": "Удачная практика", "growth_notes": "Добавить рефлексию",
    })
    assert video_response.status_code == 302
    listing = BeautifulSoup(client.get("/tutors", base_url="https://localhost").data, "html.parser")
    profile_link = listing.select_one('a[href^="/tutors/profile?"]')
    assert profile_link is not None and profile_link.get_text(strip=True) == "Учитель 1"
    profile_url = profile_link["href"].replace("&amp;", "&")
    page = client.get(profile_url, base_url="https://localhost")
    assert page.status_code == 200
    profile = BeautifulSoup(page.data, "html.parser")
    topics = [cell.get_text(strip=True) for cell in profile.select(".table-scroll tbody tr td:nth-child(3)")]
    assert "Тема 1" in topics and "Тема 2" in topics
    assert "Москва" in page.get_data(as_text=True)
    assert "name=\"telegram_username\"" in page.get_data(as_text=True)
    assert "Удачная практика" in page.get_data(as_text=True)
    assert "Добавить рефлексию" in page.get_data(as_text=True)
    assert "Открыть видео" in page.get_data(as_text=True)
    assert profile.find("a", string="Редактировать")["href"] == "/videos/1/edit"
    delete = profile.find("a", string="Удалить")
    assert urlparse(delete["href"]).path == "/videos/1/delete"
    assert parse_qs(urlparse(delete["href"]).query) == {"city": ["Москва"], "teacher": ["Учитель 1"]}
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))[1]
    response = client.post(profile_url, base_url="https://localhost", data={
        "csrf_token": csrf_token, "city": "Москва", "teacher": "Учитель 1",
        "telegram_username": "teacher_example",
    })
    assert response.status_code == 302
    profile = client.get(profile_url, base_url="https://localhost").get_data(as_text=True)
    assert '<a href="https://telegram.me/teacher_example" target="_blank"' in profile


def test_tutor_telegram_icon_is_after_name_and_absent_without_username(settings, lesson):
    run(settings, [lesson, replace(lesson, stable_id="2", teacher="Учитель 2")])
    with connection(settings.db_path) as db:
        db.execute("INSERT INTO tutor_contacts(city,teacher,telegram_username) VALUES(?,?,?)",
                   [lesson.city, lesson.teacher, "teacher_example"])
    client = create_app(settings).test_client()
    login(client, settings.password)
    page = BeautifulSoup(client.get("/tutors", base_url="https://localhost").data, "html.parser")
    row = page.select_one('[data-name="Учитель 1"]')
    cell = row.select_one("td:nth-child(2)")
    icon = cell.select_one("strong + .tutor-telegram-icon")
    assert icon and icon.select_one("svg")
    assert icon["title"] == "@teacher_example" and icon["aria-label"] == "Telegram"
    assert not icon.find_parent("a")
    assert cell.get_text(strip=True) == "Учитель 1"
    assert not row.select_one("td:nth-child(4) .tutor-telegram-icon")
    assert not page.select_one('a[href^="https://telegram.me/"]')
    assert not page.select_one('[data-name="Учитель 2"] .tutor-telegram-icon')


def test_coordinator_search_exposes_city_and_name_without_unescaped_html(settings):
    add_coordinator(settings, name='Анна <script>alert("x")</script>')
    client = create_app(settings).test_client()
    login(client, settings.password)
    response = client.get("/coordinators", base_url="https://localhost")
    page = BeautifulSoup(response.data, "html.parser")
    assert page.select_one('#coordinator-search[type="search"]')
    row = page.select_one("#coordinators-table [data-coordinator-row]")
    assert row["data-city"] == "Москва"
    assert row["data-name"] == 'Анна <script>alert("x")</script>'
    assert not row.find("script")
    assert page.select_one("[data-coordinator-no-results]").has_attr("hidden")
    assert page.select_one('script[src="/static/directory-search.js"]')


def test_teacher_report_is_sorted_by_city_and_filterable_by_course(settings, lesson):
    run(settings, [
        replace(lesson, city="Ярославль", teacher="Яков", module="Kids"),
        replace(lesson, city="Витебск", teacher="Анна", module="Matata"),
    ])
    client = create_app(settings).test_client()
    login(client, settings.password)
    all_teachers = client.get("/tutors", base_url="https://localhost").get_data(as_text=True)
    assert all_teachers.index("Витебск") < all_teachers.index("Ярославль")
    kids = client.get("/tutors?module=Kids", base_url="https://localhost").get_data(as_text=True)
    assert "Яков" in kids and "Анна" not in kids
    matata = client.get("/tutors?module=Matata", base_url="https://localhost").get_data(as_text=True)
    assert "Анна" in matata and "Яков" not in matata
    assert client.get("/tutors?module=Unknown", base_url="https://localhost").status_code == 400


def test_teacher_video_filter_offers_prefilled_add_form(settings):
    client = create_app(settings).test_client()
    login(client, settings.password)
    listing = client.get("/videos?city=Биробиджан&teacher=Лев+Поляк",
                         base_url="https://localhost").get_data(as_text=True)
    links = re.findall(r'href="([^"]+)"[^>]*>Добавить видео</a>', listing)
    href = next((link for link in links if "city=" in link), None)
    assert href
    params = parse_qs(urlparse(href.replace("&amp;", "&")).query)
    assert params == {"city": ["Биробиджан"], "teacher": ["Лев Поляк"]}
    form = client.get(href.replace("&amp;", "&"), base_url="https://localhost")
    html = form.get_data(as_text=True)
    assert 'name="city" value="Биробиджан"' in html
    assert 'name="teacher" value="Лев Поляк"' in html
    assert f'name="request_date" value="{datetime.now(settings.timezone).date().isoformat()}"' in html


def test_video_form_derives_city_and_coordinator_from_teacher(settings, lesson):
    run(settings, [lesson])
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    response = client.get("/videos/new?city=&teacher=Учитель+1", base_url="https://localhost")
    form = BeautifulSoup(response.data, "html.parser")
    assert form.select_one('input[name="city"]')["value"] == "Москва"
    assert form.select_one('input[name="city"]').has_attr("readonly")
    assert form.select_one('select[name="coordinator_id"] option[selected]')["value"] == str(coordinator_id)
    assert form.select_one('script[src="/static/video-form.js"]')
    assert client.get("/static/video-form.js", base_url="https://localhost").status_code == 200


@pytest.mark.parametrize("supplied_city", ["", "Неверный город"])
def test_video_save_resolves_city_even_without_javascript(settings, lesson, supplied_city):
    run(settings, [lesson])
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    response = client.post("/videos/new", base_url="https://localhost", data={
        "csrf_token": csrf(client), "city": supplied_city, "teacher": lesson.teacher,
        "request_date": "2026-10-07", "module": "Kids",
    })
    assert response.status_code == 302
    with connection(settings.db_path) as db:
        row = db.execute("SELECT city,teacher,coordinator_id FROM videos").fetchone()
        assert tuple(row) == (lesson.city, lesson.teacher, coordinator_id)


def test_video_teacher_change_updates_city_when_editing(settings, lesson):
    run(settings, [lesson, replace(lesson, stable_id="2", teacher="Учитель 2", city="Минск")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    fields = {"csrf_token": csrf(client), "teacher": lesson.teacher,
              "request_date": "2026-10-07", "module": "Kids"}
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 302
    fields.update(teacher="Учитель 2", city="Москва")
    assert client.post("/videos/1/edit", base_url="https://localhost", data=fields).status_code == 302
    with connection(settings.db_path) as db:
        assert db.execute("SELECT city FROM videos").fetchone()[0] == "Минск"


def test_video_ambiguous_teacher_requires_pair_and_never_guesses(settings, lesson):
    run(settings, [lesson, replace(lesson, stable_id="2", city="Минск")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    form = BeautifulSoup(client.get("/videos/new?teacher=Учитель+1",
                                    base_url="https://localhost").data, "html.parser")
    assert form.select_one('input[name="city"]')["value"] == ""
    assert {option["value"] for option in form.select("#video-teachers option")} == {
        "Учитель 1 — Москва", "Учитель 1 — Минск"}
    fields = {"csrf_token": csrf(client), "teacher": lesson.teacher,
              "request_date": "2026-10-07", "module": "Kids"}
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 400
    fields["teacher"] = "Учитель 1 — Минск"
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 302
    with connection(settings.db_path) as db:
        assert tuple(db.execute("SELECT city,teacher FROM videos").fetchone()) == ("Минск", "Учитель 1")


def test_video_city_from_existing_video_is_preserved_after_validation_error(settings, lesson):
    run(settings, [replace(lesson, teacher="Новый преподаватель")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    fields = {"csrf_token": csrf(client), "teacher": "Новый преподаватель", "city": "Москва",
              "request_date": "2026-10-07", "module": "Kids"}
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 302
    with connection(settings.db_path) as db:
        db.execute("DELETE FROM lessons")
    fields.update(city="", request_date="invalid")
    response = client.post("/videos/new", base_url="https://localhost", data=fields)
    assert response.status_code == 400
    form = BeautifulSoup(response.data, "html.parser")
    assert form.select_one('input[name="city"]')["value"] == "Москва"
    assert "Проверьте даты" in response.get_data(as_text=True)


def test_video_form_only_offers_modules_from_teacher_lessons(settings, lesson):
    run(settings, [lesson, replace(lesson, stable_id="2", module="Junior"),
                   replace(lesson, stable_id="3", teacher="Другой преподаватель", module="Matata")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    form = BeautifulSoup(client.get("/videos/new?teacher=Учитель+1",
                                    base_url="https://localhost").data, "html.parser")
    modules = form.select('select[name="module"] option')
    assert [option["value"] for option in modules] == ["Kids", "Junior"]
    assert form.select_one('select[name="module"] option[selected]')["value"] == "Kids"
    options = {option["data-teacher"]: json.loads(option["data-modules"])
               for option in form.select("#video-teachers option")}
    assert options == {"Учитель 1": ["Kids", "Junior"], "Другой преподаватель": ["Matata"]}


def test_video_modules_are_scoped_to_teacher_city(settings, lesson):
    run(settings, [lesson, replace(lesson, stable_id="2", city="Минск", module="Matata")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    form = BeautifulSoup(client.get("/videos/new?teacher=Учитель+1&city=Москва",
                                    base_url="https://localhost").data, "html.parser")
    assert [option["value"] for option in form.select('select[name="module"] option')] == ["Kids"]
    fields = {"csrf_token": csrf(client), "teacher": "Учитель 1 — Минск",
              "request_date": "2026-10-07", "module": "Kids"}
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 400
    fields["module"] = "Matata"
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 302


@pytest.mark.parametrize("module", ["Matata", "UserBasic", "Junior"])
def test_video_save_rejects_modules_not_taught_by_teacher(settings, lesson, module):
    run(settings, [lesson])
    client = create_app(settings).test_client()
    login(client, settings.password)
    response = client.post("/videos/new", base_url="https://localhost", data={
        "csrf_token": csrf(client), "teacher": lesson.teacher, "request_date": "2026-10-07",
        "module": module,
    })
    assert response.status_code == 400
    assert "Выберите модуль, который ведёт этот преподаватель" in response.get_data(as_text=True)
    form = BeautifulSoup(response.data, "html.parser")
    assert module not in [option["value"] for option in form.select('select[name="module"] option')]
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def test_video_with_no_lessons_does_not_offer_all_modules(settings):
    client = create_app(settings).test_client()
    login(client, settings.password)
    form = BeautifulSoup(client.get("/videos/new?teacher=Новое+имя&city=Москва",
                                    base_url="https://localhost").data, "html.parser")
    assert form.select_one('select[name="module"]').has_attr("disabled")
    assert [option["value"] for option in form.select('select[name="module"] option')] == [""]
    response = client.post("/videos/new", base_url="https://localhost", data={
        "csrf_token": csrf(client), "teacher": "Новое имя", "city": "Москва",
        "request_date": "2026-10-07", "module": "Matata",
    })
    assert response.status_code == 400


def test_old_video_module_is_editable_but_does_not_authorize_new_video(settings, lesson):
    run(settings, [replace(lesson, module="Matata")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    fields = {"csrf_token": csrf(client), "teacher": lesson.teacher, "city": lesson.city,
              "request_date": "2026-10-07", "module": "Matata"}
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 302
    with connection(settings.db_path) as db:
        db.execute("UPDATE lessons SET module='Kids'")
    form = BeautifulSoup(client.get("/videos/new?teacher=Учитель+1",
                                    base_url="https://localhost").data, "html.parser")
    assert [option["value"] for option in form.select('select[name="module"] option')] == ["Kids"]
    assert client.post("/videos/new", base_url="https://localhost", data=fields).status_code == 400
    fields["positive_notes"] = "Уточнённые заметки"
    assert client.post("/videos/1/edit", base_url="https://localhost", data=fields).status_code == 302
    fields["module"] = "Junior"
    assert client.post("/videos/1/edit", base_url="https://localhost", data=fields).status_code == 400
    with connection(settings.db_path) as db:
        assert tuple(db.execute("SELECT module,positive_notes FROM videos").fetchone()) == (
            "Matata", "Уточнённые заметки")


@pytest.fixture
def video_delete_client(settings, lesson):
    run(settings, [lesson])
    coordinator_id = add_coordinator(settings)
    client = create_app(settings).test_client()
    login(client, settings.password)
    for _ in range(2):
        response = client.post("/videos/new", base_url="https://localhost", data={
            "csrf_token": csrf(client), "teacher": lesson.teacher,
            "request_date": "2026-10-07", "module": "Kids",
            "coordinator_id": str(coordinator_id), "positive_notes": "Заметки о занятии",
        })
        assert response.status_code == 302
    return client


@pytest.mark.parametrize("teacher_username", ["teacher_example", ""])
@pytest.mark.parametrize("coordinator_username", ["maria_coord", ""])
def test_video_telegram_icons_match_teacher_city_and_selected_coordinator(
        settings, video_delete_client, teacher_username, coordinator_username):
    with connection(settings.db_path) as db:
        db.execute("INSERT INTO tutor_contacts(city,teacher,telegram_username) VALUES(?,?,?)",
                   ["Москва", "Учитель 1", teacher_username])
        # A namesake in another city must neither duplicate videos nor supply this contact.
        db.execute("INSERT INTO tutor_contacts(city,teacher,telegram_username) VALUES(?,?,?)",
                   ["Минск", "Учитель 1", "other_city_user"])
        db.execute("UPDATE coordinators SET telegram_username=?", [coordinator_username])
    response = video_delete_client.get("/videos?city=Москва&teacher=Учитель+1", base_url="https://localhost")
    assert response.status_code == 200
    page = BeautifulSoup(response.data, "html.parser")
    rows = page.select(".table-scroll tbody tr")
    assert len(rows) == 2
    for row in rows:
        cells = row.find_all("td", recursive=False)
        for cell, username in ((cells[2], teacher_username), (cells[5], coordinator_username)):
            icon = cell.select_one(".tutor-telegram-icon")
            assert (icon is not None) == bool(username)
            if username:
                assert icon["title"] == "@" + username and icon.select_one("svg")
                assert not icon.find_parent("a")


def test_video_telegram_icons_absent_for_missing_contacts(settings, video_delete_client):
    with connection(settings.db_path) as db:
        db.execute("UPDATE videos SET coordinator_id=NULL")
    page = BeautifulSoup(video_delete_client.get("/videos", base_url="https://localhost").data, "html.parser")
    assert not page.select_one(".tutor-telegram-icon")
    assert len(page.select(".table-scroll tbody tr")) == 2


def test_video_delete_links_show_confirmation_and_preserve_cancel_destination(settings, video_delete_client):
    client = video_delete_client
    listing = BeautifulSoup(client.get("/videos?city=Москва&teacher=Учитель+1",
                                        base_url="https://localhost").data, "html.parser")
    link = listing.select_one('a[href^="/videos/1/delete"]')
    assert link.get_text(strip=True) == "Удалить"
    assert parse_qs(urlparse(link["href"]).query) == {"city": ["Москва"], "teacher": ["Учитель 1"]}
    page = client.get(link["href"], base_url="https://localhost")
    assert page.status_code == 200
    confirmation = BeautifulSoup(page.data, "html.parser")
    assert "Учитель 1" in confirmation.get_text() and "07.10.2026" in confirmation.get_text()
    assert confirmation.select_one('form[method="post"] input[name="csrf_token"]')
    cancel = confirmation.find("a", string="Отмена")
    assert parse_qs(urlparse(cancel["href"]).query) == {"city": ["Москва"], "teacher": ["Учитель 1"]}
    edit = BeautifulSoup(client.get("/videos/1/edit", base_url="https://localhost").data, "html.parser")
    edit_link = edit.find("a", string="Удалить видео")
    edit_confirmation = BeautifulSoup(client.get(edit_link["href"],
                                                 base_url="https://localhost").data, "html.parser")
    assert edit_confirmation.find("a", string="Отмена")["href"] == "/videos/1/edit"
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2


def test_video_delete_requires_login(settings, video_delete_client):
    anonymous = create_app(settings).test_client()
    response = anonymous.post("/videos/1/delete", base_url="https://localhost",
                              data={"csrf_token": csrf(anonymous)})
    assert response.status_code == 302 and response.location == "/login"
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2


@pytest.mark.parametrize("token", ["", "invalid"])
def test_video_delete_rejects_invalid_csrf(settings, video_delete_client, token):
    response = video_delete_client.post("/videos/1/delete", base_url="https://localhost",
                                        data={"csrf_token": token})
    assert response.status_code == 400
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2


def test_video_delete_removes_only_selected_record_and_updates_counts(settings, video_delete_client):
    client = video_delete_client
    response = client.post("/videos/1/delete?city=Москва&teacher=Учитель+1",
                           base_url="https://localhost", data={"csrf_token": csrf(client)})
    assert response.status_code == 302
    assert urlparse(response.location).path == "/videos"
    assert parse_qs(urlparse(response.location).query) == {"city": ["Москва"], "teacher": ["Учитель 1"]}
    with connection(settings.db_path) as db:
        assert [row[0] for row in db.execute("SELECT id FROM videos")] == [2]
        assert db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM coordinators").fetchone()[0] == 1
    tutors = BeautifulSoup(client.get("/tutors", base_url="https://localhost").data, "html.parser")
    assert tutors.select_one(".count-link").get_text(strip=True) == "1"
    assert client.get("/videos/1/edit", base_url="https://localhost").status_code == 404
    assert client.get("/videos/1/report.xlsx", base_url="https://localhost").status_code == 404
    assert client.get("/videos/2/edit", base_url="https://localhost").status_code == 200
    assert client.post("/videos/1/delete", base_url="https://localhost",
                       data={"csrf_token": csrf(client)}).status_code == 404


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_video_delete_missing_record_returns_404(video_delete_client, method):
    response = video_delete_client.open("/videos/999/delete", method=method,
                                        base_url="https://localhost",
                                        data={"csrf_token": csrf(video_delete_client)} if method == "POST" else None)
    assert response.status_code == 404


def test_video_excel_contains_reference_fields_and_safe_text(settings, lesson):
    run(settings, [replace(lesson, teacher="Елена Пример")])
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
def test_video_rejects_non_web_links(settings, lesson, url):
    run(settings, [replace(lesson, teacher="Елена")])
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
