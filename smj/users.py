import secrets

from flask import abort, redirect, render_template, request, url_for
from flask_login import UserMixin
from werkzeug.security import generate_password_hash

from .db import connection
from .rate_limit import normalize_login, valid_login

ROLES = {"admin", "guest"}


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.login = row["login"]
        self.role = row["role"]
        self.session_id = row["session_id"]

    def get_id(self):
        return self.session_id


def initialize_users(path, legacy_password):
    with connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            return
        if len(legacy_password) < 32:
            raise ValueError("APP_PASSWORD must have at least 32 characters for the initial admin")
        db.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            login TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'guest')) DEFAULT 'guest',
            session_id TEXT NOT NULL UNIQUE
        )""")
        db.execute("""INSERT INTO users(login,password_hash,role,session_id) VALUES(?,?,?,?)""",
                   ("admin", generate_password_hash(legacy_password), "admin", secrets.token_urlsafe(32)))


def register_user_routes(app, path):
    def fields():
        return {"login": normalize_login(request.form.get("login", "")),
                "role": request.form.get("role", "guest")}

    def validation_error(values):
        if not valid_login(values["login"]):
            return "Логин: от 3 до 32 латинских букв, цифр, точек, дефисов или знаков _."
        if values["role"] not in ROLES:
            return "Выберите допустимую роль."
        return None

    def get_user(db, user_id):
        user = db.execute("SELECT id,login,role FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            abort(404)
        return user

    def last_admin(db, user):
        return user["role"] == "admin" and db.execute(
            "SELECT 1 FROM users WHERE role='admin' AND id<>? LIMIT 1", (user["id"],)
        ).fetchone() is None

    def render_settings(**context):
        with connection(path) as db:
            users = db.execute("SELECT id,login,role FROM users ORDER BY login").fetchall()
        return render_template("settings.html", title="Настройки", users=users, **context)

    @app.get("/settings")
    def settings():
        return render_settings()

    @app.post("/settings/users")
    def user_new():
        values = fields()
        error = validation_error(values)
        if error:
            return render_settings(values=values, error=error), 400
        password = secrets.token_urlsafe(32)
        password_hash = generate_password_hash(password)
        with connection(path) as db:
            result = db.execute("""INSERT INTO users(login,password_hash,role,session_id)
                VALUES(?,?,?,?) ON CONFLICT(login) DO NOTHING""",
                (values["login"], password_hash, values["role"], secrets.token_urlsafe(32)))
            created = result.rowcount == 1
        if not created:
            return render_settings(values=values, error="Этот логин уже занят."), 409
        return render_settings(generated_login=values["login"], generated_password=password)

    @app.route("/settings/users/<int:user_id>/edit", methods=["GET", "POST"])
    def user_edit(user_id):
        error, status = None, 200
        with connection(path) as db:
            if request.method == "POST":
                db.execute("BEGIN IMMEDIATE")
            user = get_user(db, user_id)
            values = dict(user)
            if request.method == "POST":
                values = fields()
                error = validation_error(values)
                if error:
                    status = 400
                elif values["role"] == "guest" and last_admin(db, user):
                    error, status = "Нельзя изменить роль последнего администратора.", 409
                elif db.execute("SELECT 1 FROM users WHERE login=? AND id<>?",
                                (values["login"], user_id)).fetchone():
                    error, status = "Этот логин уже занят.", 409
                else:
                    # A new session identifier revokes every previously issued cookie for the user.
                    db.execute("""UPDATE users SET login=?,role=?,
                        session_id=CASE WHEN role<>? THEN ? ELSE session_id END WHERE id=?""",
                        (values["login"], values["role"], values["role"],
                         secrets.token_urlsafe(32), user_id))
                    return redirect(url_for("settings"))
        return render_template("user_form.html", title="Редактировать пользователя",
                               user=user, values=values, error=error), status

    @app.route("/settings/users/<int:user_id>/delete", methods=["GET", "POST"])
    def user_delete(user_id):
        error, status = None, 200
        with connection(path) as db:
            if request.method == "POST":
                db.execute("BEGIN IMMEDIATE")
            user = get_user(db, user_id)
            if request.method == "POST":
                if last_admin(db, user):
                    error, status = "Нельзя удалить последнего администратора.", 409
                else:
                    db.execute("DELETE FROM users WHERE id=?", (user_id,))
                    return redirect(url_for("settings"))
        return render_template("user_delete.html", title="Удалить пользователя",
                               user=user, error=error), status

    @app.post("/settings/users/<int:user_id>/reset-password")
    def user_reset_password(user_id):
        password = secrets.token_urlsafe(32)
        password_hash = generate_password_hash(password)
        with connection(path) as db:
            db.execute("BEGIN IMMEDIATE")
            user = get_user(db, user_id)
            db.execute("UPDATE users SET password_hash=?,session_id=? WHERE id=?",
                       (password_hash, secrets.token_urlsafe(32), user_id))
        return render_settings(generated_login=user["login"], generated_password=password)
