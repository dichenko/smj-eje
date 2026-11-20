from dataclasses import replace
from urllib.parse import urlencode

import pytest
from bs4 import BeautifulSoup
from werkzeug.security import check_password_hash

from smj.db import connection, initialize
from conftest import create_app
from test_sync import run
from test_videos_coordinators import add_coordinator
from test_web import login


BASE_URL = "https://localhost"


def get(client, path):
    return client.get(path, base_url=BASE_URL)


def post(client, path, **fields):
    page = get(client, "/weekly")
    csrf = BeautifulSoup(page.data, "html.parser").select_one('input[name="csrf_token"]')["value"]
    return client.post(path, data={"csrf_token": csrf, **fields}, base_url=BASE_URL)


def user_row(settings, name):
    with connection(settings.db_path) as db:
        row = db.execute("SELECT * FROM users WHERE login=?", [name]).fetchone()
    return dict(row) if row else None


def user_path(user, action):
    return f"/settings/users/{user['id']}/{action}"


def generated_password(response):
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    output = BeautifulSoup(response.data, "html.parser").select_one("output#generated-password")
    assert output is not None
    password = output.get_text(strip=True)
    assert len(password) >= 32
    return password


def create_user(client, settings, name="viewer", **fields):
    password = generated_password(post(client, "/settings/users", login=name, **fields))
    return user_row(settings, name), password


@pytest.fixture
def admin(settings):
    app = create_app(settings)
    client = app.test_client()
    assert login(client, settings.password).status_code == 302
    return app, client


def test_migration_is_additive_and_bootstraps_existing_password_once(settings):
    initialize(settings.db_path)
    coordinator_id = add_coordinator(settings)
    with connection(settings.db_path) as db:
        db.execute("DROP TABLE IF EXISTS users")

    app = create_app(settings)
    original = user_row(settings, "admin")
    assert original["role"] == "admin"
    assert original["password_hash"] != settings.password
    assert check_password_hash(original["password_hash"], settings.password)
    client = app.test_client()
    assert login(client, settings.password).status_code == 302
    cookie = client.get_cookie("smj_session")

    changed_password = "C" * 48
    for value in (changed_password, "", settings.password):
        restarted = create_app(replace(settings, password=value))
        assert user_row(settings, "admin") == original
        with connection(settings.db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
            assert db.execute("SELECT id FROM coordinators").fetchone()[0] == coordinator_id
        existing = restarted.test_client()
        existing.set_cookie("smj_session", cookie.value)
        assert get(existing, "/api/lessons").status_code == 200

    restarted = create_app(replace(settings, password=changed_password))
    assert login(restarted.test_client(), changed_password).status_code == 401
    assert login(restarted.test_client(), settings.password).status_code == 302


def test_settings_crud_and_one_time_password_display(admin, settings):
    app, client = admin
    assert get(app.test_client(), "/settings").status_code == 302
    page = get(client, "/weekly")
    assert BeautifulSoup(page.data, "html.parser").select_one('a[href="/settings"]')
    listing = get(client, "/settings")
    assert listing.status_code == 200

    viewer, password = create_user(client, settings)
    other, other_password = create_user(client, settings, "another_user")
    assert password != other_password
    assert viewer["role"] == "guest"
    assert viewer["password_hash"] != password
    assert check_password_hash(viewer["password_hash"], password)
    edit_path = user_path(viewer, "edit")
    delete_path = user_path(viewer, "delete")
    assert get(client, delete_path).status_code == 200
    for path in ("/settings", edit_path, delete_path):
        page = get(client, path)
        assert page.status_code == 200
        assert b'id="generated-password"' not in page.data
        assert password.encode() not in page.data
        assert viewer["password_hash"].encode() not in page.data
    with client.session_transaction(base_url=BASE_URL) as session:
        assert password not in repr(dict(session))

    assert post(client, edit_path, login=other["login"], role="admin").status_code == 409
    assert post(client, edit_path, login="", role="admin").status_code == 400
    assert post(client, edit_path, login="new_name", role="owner").status_code == 400
    assert user_row(settings, "viewer") == viewer
    changed = post(client, edit_path, login="new_name", role="admin")
    assert changed.status_code in {302, 303}
    assert user_row(settings, "viewer") is None
    renamed = user_row(settings, "new_name")
    assert renamed["id"] == viewer["id"]
    assert renamed["role"] == "admin"
    assert renamed["password_hash"] == viewer["password_hash"]
    assert user_row(settings, "another_user") == other

    removed = post(client, delete_path)
    assert removed.status_code in {302, 303}
    assert user_row(settings, "new_name") is None
    assert user_row(settings, "another_user") == other


@pytest.mark.parametrize(("fields", "expected"), [
    ({"login": ""}, 400),
    ({"login": "new_user", "role": "owner"}, 400),
    ({"login": "admin"}, 409),
    ({"login": "ADMIN"}, 409),
])
def test_creation_rejects_bad_fields_and_duplicate_login(admin, settings, fields, expected):
    _, client = admin
    response = post(client, "/settings/users", **fields)
    assert response.status_code == expected
    assert b'id="generated-password"' not in response.data
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_guest_can_read_reports_and_download_but_cannot_mutate(admin, settings, lesson):
    app, client = admin
    viewer, password = create_user(client, settings)
    run(settings, [lesson])
    coordinator_id = add_coordinator(settings)
    with connection(settings.db_path) as db:
        video_id = db.execute("""
            INSERT INTO videos (city, teacher, coordinator_id, module, request_date,
                                created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [lesson.city, lesson.teacher, coordinator_id, lesson.module,
              lesson.date, lesson.date, lesson.date]).lastrowid
    profile = "/tutors/profile?" + urlencode({"city": lesson.city, "teacher": lesson.teacher})
    guest = app.test_client()
    assert login(guest, password, viewer["login"]).status_code == 302
    for path in ("/weekly", "/lessons", "/tutors", profile, "/coordinators", "/cities", "/videos",
                 "/api/lessons", "/api/status"):
        assert get(guest, path).status_code == 200, path
    workbook = get(guest, f"/videos/{video_id}/report.xlsx")
    assert workbook.status_code == 200
    assert workbook.data.startswith(b"PK\x03\x04")
    assert "attachment" in workbook.headers["Content-Disposition"]

    admin_only_pages = ("/settings", user_path(viewer, "edit"), user_path(viewer, "delete"),
                        "/coordinators/new", f"/coordinators/{coordinator_id}/edit", "/videos/new",
                        f"/videos/{video_id}/edit", f"/videos/{video_id}/delete")
    for path in admin_only_pages:
        assert get(guest, path).status_code == 403, path
    for page_path in ("/weekly", "/videos", "/coordinators", profile):
        page = BeautifulSoup(get(guest, page_path).data, "html.parser")
        for path in admin_only_pages:
            assert not page.select_one(f'a[href="{path}"], a[href^="{path}?"]'), path
    assert not BeautifulSoup(get(guest, profile).data, "html.parser").select_one(
        '[name="telegram_username"]')
    for path, data in (
        ("/settings/users", {"login": "unauthorized_user", "role": "admin"}),
        (user_path(viewer, "edit"), {"login": "viewer", "role": "admin"}),
        (user_path(viewer, "reset-password"), {}),
        (user_path(viewer, "delete"), {}),
        ("/tutors/profile", {"city": lesson.city, "teacher": lesson.teacher,
                             "telegram_username": "new_contact"}),
        ("/coordinators/new", {"city": lesson.city, "name": "Guest addition"}),
        (f"/coordinators/{coordinator_id}/edit", {"city": lesson.city, "name": "Guest edit"}),
        ("/videos/new", {"city": lesson.city, "teacher": lesson.teacher, "module": lesson.module,
                         "request_date": lesson.date}),
        (f"/videos/{video_id}/edit", {"city": lesson.city, "teacher": lesson.teacher,
                                     "module": lesson.module, "request_date": "2026-10-02"}),
        (f"/videos/{video_id}/delete", {}),
    ):
        assert post(guest, path, **data).status_code == 403, path
    assert user_row(settings, "viewer") == viewer
    with connection(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM tutor_contacts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM coordinators").fetchone()[0] == 1
        assert db.execute("SELECT request_date FROM videos").fetchall()[0][0] == lesson.date
    assert post(guest, "/logout").status_code == 302
    assert get(guest, "/api/lessons").status_code == 401


def test_role_change_reset_and_delete_revoke_only_target_sessions(admin, settings):
    app, admin_client = admin
    operator, password = create_user(admin_client, settings, "operator", role="admin")
    old_session = app.test_client()
    assert login(old_session, password, "operator").status_code == 302
    assert get(old_session, "/settings").status_code == 200

    result = post(admin_client, user_path(operator, "edit"), login="operator", role="guest")
    assert result.status_code in {302, 303}
    assert get(old_session, "/api/lessons").status_code == 401
    assert get(admin_client, "/settings").status_code == 200
    demoted = app.test_client()
    assert login(demoted, password, "operator").status_code == 302
    assert get(demoted, "/settings").status_code == 403

    reset_path = user_path(operator, "reset-password")
    assert get(admin_client, reset_path).status_code == 405
    new_password = generated_password(post(admin_client, reset_path))
    assert new_password != password
    row = user_row(settings, "operator")
    assert row["password_hash"] != new_password
    assert check_password_hash(row["password_hash"], new_password)
    assert new_password.encode() not in get(admin_client, "/settings").data
    assert new_password.encode() not in get(admin_client, user_path(operator, "edit")).data
    assert get(demoted, "/api/lessons").status_code == 401
    assert login(app.test_client(), password, "operator").status_code == 401
    after_reset = app.test_client()
    assert login(after_reset, new_password, "operator").status_code == 302

    assert post(admin_client, user_path(operator, "delete")).status_code in {302, 303}
    assert get(after_reset, "/api/lessons").status_code == 401
    assert get(admin_client, "/settings").status_code == 200


def test_settings_forms_require_csrf(admin, settings):
    app, client = admin
    row = user_row(settings, "admin")
    for path in ("/settings/users", user_path(row, "edit"), user_path(row, "delete"),
                 user_path(row, "reset-password")):
        for data in ({}, {"csrf_token": "invalid"}):
            assert client.post(path, data=data, base_url=BASE_URL).status_code == 400
    assert user_row(settings, "admin") == row
    assert get(app.test_client(), "/api/lessons").status_code == 401


def test_last_admin_cannot_be_deleted_or_demoted(admin, settings):
    _, client = admin
    row = user_row(settings, "admin")
    create_user(client, settings)
    assert post(client, user_path(row, "delete")).status_code == 409
    assert post(client, user_path(row, "edit"), login="admin", role="guest").status_code == 409
    assert user_row(settings, "admin") == row
    assert get(client, "/settings").status_code == 200
