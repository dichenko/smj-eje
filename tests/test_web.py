from dataclasses import replace
import re

import pytest

from smj.web import create_app
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
    "/kids", "/userbasic", "/junior", "/tutors", "/cities", "/lessons"])
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
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]


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
