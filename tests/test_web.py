from dataclasses import replace
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from smj.web import create_app
from smj.db import connection
from test_sync import run


def token(client):
    html = client.get("/login", base_url="https://localhost").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html)[1]


def login(client, password):
    return client.post("/login", base_url="https://localhost",
                       data={"csrf_token": token(client), "password": password})


@pytest.fixture
def app(settings):
    app = create_app(settings)
    app.config["TESTING"] = True
    return app


@pytest.mark.parametrize("path", ["/", "/weekly", "/weekly/2026-09-28", "/matata",
    "/kids", "/userbasic", "/junior", "/tutors", "/tutors/profile?city=Москва&teacher=Учитель+1",
    "/cities", "/lessons"])
def test_every_report_requires_auth(app, path):
    response = app.test_client().get(path, base_url="https://localhost")
    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


@pytest.mark.parametrize("path", ["/api/lessons", "/api/status", "/api/unknown"])
def test_all_api_paths_require_auth(app, path):
    assert app.test_client().get(path, base_url="https://localhost").status_code == 401


def test_login_logout_csrf_and_cookie_security(app, settings):
    client = app.test_client()
    assert client.post("/login", data={"password": settings.password}).status_code == 400
    response = login(client, settings.password)
    assert response.status_code == 302
    cookie = response.headers["Set-Cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert client.get("/weekly", base_url="https://localhost").status_code == 200
    assert client.get("/logout", base_url="https://localhost").status_code == 405
    with client.session_transaction(base_url="https://localhost") as session:
        csrf = session["csrf"]
    assert client.post("/logout", base_url="https://localhost", data={"csrf_token": csrf}).status_code == 302
    assert client.get("/api/lessons", base_url="https://localhost").status_code == 401


def test_shared_rate_limit_survives_restart(settings):
    client = create_app(settings).test_client()
    for _ in range(5):
        assert login(client, "bad").status_code == 401
    restarted = create_app(settings).test_client()
    response = login(restarted, settings.password)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"


def test_password_rotation_invalidates_existing_sessions(settings):
    app = create_app(settings)
    client = app.test_client()
    login(client, settings.password)
    cookie = client.get_cookie("smj_session")
    changed = create_app(replace(settings, password="C" * 48)).test_client()
    changed.set_cookie("smj_session", cookie.value)
    assert changed.get("/api/lessons", base_url="https://localhost").status_code == 401


def test_unsafe_source_content_escaped_in_reports(settings, lesson):
    run(settings, [replace(lesson, teacher='<script>alert("x")</script>')])
    client = create_app(settings).test_client()
    login(client, settings.password)
    html = client.get("/lessons", base_url="https://localhost").get_data(as_text=True)
    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;" in html
    assert client.get("/api/lessons", base_url="https://localhost").json["pagination"]["total_count"] == 1


@pytest.mark.parametrize("query", ["per_page=0", "per_page=1000", "page=-1", "page=x",
    "module=unknown", "start=bad", "start=2026-10-05&end=2026-09-01"])
def test_bad_filters_fail_safely(app, settings, query):
    client = app.test_client()
    login(client, settings.password)
    assert client.get("/api/lessons?" + query, base_url="https://localhost").status_code == 400


@pytest.mark.parametrize("path", ["/lessons", "/kids", "/matata", "/junior", "/userbasic",
    "/tutors", "/cities", "/weekly/2026-09-28"])
def test_reports_render_with_data(app, settings, lesson, path):
    run(settings, [lesson])
    client = app.test_client()
    login(client, settings.password)
    response = client.get(path, base_url="https://localhost")
    assert response.status_code == 200
    assert "Обновлено" in response.get_data(as_text=True)
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in response.headers["Content-Security-Policy"]


def test_source_unavailable_does_not_block_reports(settings, lesson):
    run(settings, [lesson])
    client = create_app(settings).test_client()
    login(client, settings.password)
    html = client.get("/weekly/2026-09-28", base_url="https://localhost").get_data(as_text=True)
    assert "Тема 1" in html
    assert "Учитель 1" in html


def test_fail_closed_without_long_secrets(settings):
    with pytest.raises(ValueError):
        create_app(replace(settings, password=""))
    with pytest.raises(ValueError):
        create_app(replace(settings, secret_key=""))


def test_public_health_discloses_no_data_and_is_blocked(app):
    response = app.test_client().get("/healthz", environ_base={"REMOTE_ADDR": "203.0.113.1"})
    assert response.status_code == 404


def test_svg_favicon_is_linked_and_served(app, settings):
    client = app.test_client()
    login(client, settings.password)
    page = client.get("/tutors", base_url="https://localhost").get_data(as_text=True)
    assert 'rel="icon" type="image/svg+xml" href="/static/favicon.svg"' in page
    icon = client.get("/static/favicon.svg", base_url="https://localhost")
    assert icon.status_code == 200
    assert b">S</text>" in icon.data and b">J</text>" in icon.data


def test_tutor_search_is_realtime_and_scripts_are_allowed_from_static(app, settings, lesson):
    run(settings, [lesson])
    client = app.test_client()
    login(client, settings.password)
    page = client.get("/tutors", base_url="https://localhost").get_data(as_text=True)
    assert 'id="tutor-search" type="search"' in page
    assert 'data-tutor-row data-name="Учитель 1"' in page
    assert 'data-city="Москва"' in page
    assert "Поиск по городу, имени или фамилии" in page
    assert "tutors-search.js" in page
    script = client.get("/static/tutors-search.js", base_url="https://localhost")
    assert script.status_code == 200
    assert b"addEventListener(\"input\"" in script.data
    assert b"word.startsWith(term)" in script.data


def test_city_search_choices_include_teachers_for_switching_cities(settings, lesson):
    run(settings, [lesson, replace(lesson, stable_id="2", city="Минск", teacher="Анна Иванова")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    response = client.get("/cities?city=Москва&teacher=Учитель+1", base_url="https://localhost")
    page = BeautifulSoup(response.data, "html.parser")
    assert page.select_one('input[name="city"][type="search"]')["value"] == "Москва"
    assert page.select_one('input[name="teacher"][type="search"]')["value"] == "Учитель 1"
    choices = {(option["value"], option["data-city"]) for option in page.select("#teacher-search-options option")}
    assert choices == {("Учитель 1", "Москва"), ("Анна Иванова", "Минск")}
    assert page.select_one('script[src="/static/directory-search.js"]')
    filtered = client.get("/cities?city=Минск&teacher=Анна+Иванова", base_url="https://localhost")
    rows = BeautifulSoup(filtered.data, "html.parser").select(".table-scroll tbody tr")
    assert len(rows) == 1 and "Анна Иванова" in rows[0].get_text()
    other_report = BeautifulSoup(client.get("/kids", base_url="https://localhost").data, "html.parser")
    assert other_report.select_one('select[name="city"]')


def test_city_report_teacher_links_and_icons_use_each_rows_city(settings, lesson):
    run(settings, [replace(lesson, city="Томск"), replace(lesson, stable_id="2"),
                   replace(lesson, stable_id="3", city="Томск", teacher="Учитель 2")])
    with connection(settings.db_path) as db:
        db.executemany("INSERT INTO tutor_contacts(city,teacher,telegram_username) VALUES(?,?,?)", [
            ("Томск", "Учитель 1", "tomsk_teacher"), ("Москва", "Учитель 1", "moscow_teacher")])
    client = create_app(settings).test_client()
    login(client, settings.password)
    for city in ("", "Томск"):
        page = BeautifulSoup(client.get("/cities", query_string={"city": city},
                                       base_url="https://localhost").data, "html.parser")
        rows = page.select(".table-scroll tbody tr")
        assert len(rows) == (2 if city else 3)
        for row in rows:
            cells = row.find_all("td", recursive=False)
            row_city = cells[3].get_text(strip=True)
            link = cells[4].find("a")
            assert link and urlsplit(link["href"]).path == "/tutors/profile"
            assert parse_qs(urlsplit(link["href"]).query) == {
                "city": [row_city], "teacher": [link.get_text(strip=True)]}
            assert client.get(link["href"], base_url="https://localhost").status_code == 200
            icon = cells[4].select_one(".tutor-telegram-icon")
            if link.get_text(strip=True) == "Учитель 1":
                assert icon and icon["title"] == ("@tomsk_teacher" if row_city == "Томск" else "@moscow_teacher")
                assert icon.name == "a" and icon["href"] == "https://telegram.me/" + icon["title"][1:]
                assert icon["target"] == "_blank" and set(icon["rel"]) == {"noopener", "noreferrer"}
            else:
                assert icon is None


def test_non_ascii_csrf_rejected_without_server_error(app):
    client = app.test_client()
    token(client)
    assert client.post("/login", base_url="https://localhost",
                       data={"csrf_token": "неверный", "password": "bad"}).status_code == 400


@pytest.mark.parametrize("week", ["9999-12-31", "2026-08-01", "bad"])
def test_week_outside_period_rejected(app, settings, week):
    client = app.test_client()
    login(client, settings.password)
    assert client.get("/weekly/" + week, base_url="https://localhost").status_code == 400


def test_lesson_table_uses_consistent_alternating_week_colors(settings, lesson):
    lessons = [replace(lesson, stable_id="week-oct-5", date="2026-10-05", topic="Понедельник"),
               replace(lesson, stable_id="week-oct-4", date="2026-10-04", topic="Воскресенье"),
               replace(lesson, stable_id="week-sep-28", date="2026-09-28", topic="Предыдущая неделя")]
    run(settings, lessons)
    app = create_app(settings)
    client = app.test_client()
    login(client, settings.password)
    response = client.get("/cities?teacher=Учитель 1", base_url="https://localhost")
    rows = BeautifulSoup(response.data, "html.parser").select(".table-scroll tbody tr")
    bands = [row.get("class", [])[0] for row in rows]
    assert len(bands) == 3
    # The report sorts newest first: Oct 5 is a new week; Oct 4 and Sep 28 share one.
    assert bands[0] != bands[1]
    assert bands[1] == bands[2]
    week_band = app.jinja_env.filters["week_band"]
    assert week_band("2026-12-28") != week_band("2027-01-04")


def test_numbered_pagination_keeps_filters_and_shows_nearby_pages(settings, lesson):
    lessons = [replace(lesson, stable_id=f"page-{number}", topic=f"Занятие {number}")
               for number in range(120)]
    run(settings, lessons)
    client = create_app(settings).test_client()
    login(client, settings.password)

    response = client.get(
        "/cities?teacher=Учитель+1&start=2026-09-01&per_page=10&page=6",
        base_url="https://localhost",
    )
    pagination = BeautifulSoup(response.data, "html.parser").select_one("nav.pagination")
    assert pagination is not None
    page_items = pagination.select(".page-number")
    assert [item.get_text(strip=True) for item in page_items] == ["1", "4", "5", "6", "7", "8", "12"]
    assert pagination.select_one('[aria-current="page"]').get_text(strip=True) == "6"
    assert len(pagination.select(".pagination-ellipsis")) == 2
    for item in page_items:
        if item.name == "a":
            params = parse_qs(urlsplit(item["href"]).query)
            assert params["teacher"] == ["Учитель 1"]
            assert params["start"] == ["2026-09-01"]
            assert params["per_page"] == ["10"]
