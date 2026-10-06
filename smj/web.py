import calendar
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from .config import MODULES, Settings
from .db import connection, distinct_values, initialize, query_lessons, sync_status, utc_now
from .excel import video_report


def create_app(settings=None):
    settings = settings or Settings.from_env()
    settings.validate_web()
    initialize(settings.db_path)
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_NAME="smj_session",
        SESSION_COOKIE_SECURE=settings.production,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=settings.session_days),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=8192,
        SETTINGS=settings,
    )
    if settings.production:
        # Exactly one trusted proxy: system Caddy; the upstream must remain loopback-only.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    auth_version = hmac.new(settings.secret_key.encode(), settings.password.encode(),
                            hashlib.sha256).hexdigest()

    @app.template_filter("lesson_word")
    def lesson_word(number):
        if 11 <= number % 100 <= 14:
            return "занятий"
        return "занятие" if number % 10 == 1 else "занятия" if number % 10 in {2, 3, 4} else "занятий"

    def authenticated():
        value = session.get("auth_version", "")
        return isinstance(value, str) and hmac.compare_digest(value, auth_version)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def protect():
        if request.method == "POST":
            supplied, expected = request.form.get("csrf_token", ""), session.get("csrf", "")
            if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                abort(400, "Invalid form token. Reload the page and retry.")
        if request.endpoint in {"login", "static", "health"}:
            return None
        if not authenticated():
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("login"))
        return None

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'none'; img-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.context_processor
    def context():
        return {"csrf_token": csrf_token, "is_authenticated": authenticated(),
                "modules": MODULES, "data_start": settings.start_date,
                "sync": sync_status(settings) if authenticated() else None}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if authenticated():
            return redirect(url_for("weekly"))
        error, status = None, 200
        if request.method == "POST":
            now = time.time()
            ip = request.remote_addr or "unknown"
            # SQLite BEGIN IMMEDIATE makes the shared rate limit atomic across all workers.
            with connection(settings.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", [now - 900])
                total = db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
                count = db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=?", [ip]).fetchone()[0]
                if count >= 5 or total >= 50:
                    error, status = "Слишком много попыток. Повторите через 15 минут.", 429
                elif hmac.compare_digest(
                    hashlib.sha256(request.form.get("password", "").encode()).digest(),
                    hashlib.sha256(settings.password.encode()).digest(),
                ):
                    db.execute("DELETE FROM login_attempts WHERE ip=?", [ip])
                    session.clear()
                    session.permanent = True
                    session["auth_version"] = auth_version
                    session["csrf"] = secrets.token_urlsafe(32)
                    return redirect(url_for("weekly"))
                else:
                    db.execute("INSERT INTO login_attempts VALUES (?,?)", [ip, now])
                    error, status = "Неверный пароль.", 401
        response = app.make_response((render_template("login.html", error=error), status))
        if status == 429:
            response.headers["Retry-After"] = "900"
        return response

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def health():
        # Caddyfile blocks this route publicly. Direct loopback checks use no forwarded headers.
        if request.remote_addr not in {"127.0.0.1", "::1"} and not authenticated():
            abort(404)
        with connection(settings.db_path) as db:
            db.execute("SELECT 1 FROM lessons LIMIT 1")
        return jsonify(status="ok")

    @app.get("/")
    def index():
        return redirect(url_for("weekly"))

    def filters(module=None):
        result = {field: request.args.get(field, "").strip() for field in
                  ("city", "teacher", "group_name", "start", "end")}
        result["module"] = module or request.args.get("module", "")
        if result["module"] and result["module"] not in MODULES:
            abort(400, "Unknown module")
        if any(len(v) > 200 for v in result.values()):
            abort(400, "Filter too long")
        try:
            for field in ("start", "end"):
                if result[field]:
                    result[field] = date.fromisoformat(result[field]).isoformat()
            if result["start"] and result["end"] and result["start"] > result["end"]:
                raise ValueError
        except ValueError:
            abort(400, "Invalid date range")
        result["start"] = max(result["start"] or settings.start_date.isoformat(),
                              settings.start_date.isoformat())
        return result

    def pagination():
        try:
            page, size = int(request.args.get("page", "1")), int(request.args.get("per_page", "25"))
        except ValueError:
            abort(400, "Invalid pagination")
        if not 1 <= page <= 1_000_000 or not 1 <= size <= 100:
            abort(400, "Invalid pagination")
        return page, size

    def report(title, module=None):
        selected = filters(module)
        page, per_page = pagination()
        with connection(settings.db_path) as db:
            lessons, total = query_lessons(db, selected, page, per_page)
            cities = distinct_values(db, "city")
            teachers = distinct_values(db, "teacher", selected["city"])
            groups = distinct_values(db, "group_name", selected["city"])
        pages = max(1, (total + per_page - 1) // per_page)
        links = {"prev": url_for(request.endpoint, **{**request.args.to_dict(), "page": page - 1}),
                 "next": url_for(request.endpoint, **{**request.args.to_dict(), "page": page + 1})}
        return render_template("report.html", title=title, selected=selected, lessons=lessons,
                               total=total, page=page, pages=pages, per_page=per_page,
                               cities=cities, teachers=teachers, groups=groups, links=links,
                               locked_module=module)

    @app.get("/lessons")
    def lessons():
        return report("Все занятия")

    @app.get("/tutors")
    def tutors():
        with connection(settings.db_path) as db:
            teacher_map = {}
            for row in db.execute("SELECT DISTINCT city,teacher,module FROM lessons"):
                key = (row["city"], row["teacher"])
                teacher_map.setdefault(key, {"city": row["city"], "teacher": row["teacher"],
                                              "modules": set(), "video_count": 0})
                teacher_map[key]["modules"].add(row["module"])
            for row in db.execute("""
                SELECT city,teacher,module,COUNT(*) AS video_count FROM videos
                GROUP BY city,teacher,module
            """):
                key = (row["city"], row["teacher"])
                teacher = teacher_map.setdefault(key, {"city": row["city"], "teacher": row["teacher"],
                                                       "modules": set(), "video_count": 0})
                teacher["modules"].add(row["module"])
                teacher["video_count"] += row["video_count"]
        teachers = sorted(teacher_map.values(), key=lambda row: (row["city"].casefold(),
                                                                  row["teacher"].casefold()))
        module_order = {name: index for index, name in enumerate(MODULES)}
        for teacher in teachers:
            teacher["modules"] = sorted(teacher["modules"], key=lambda name: module_order.get(name, 99))
        return render_template("tutors.html", title="Преподаватели", teachers=teachers)

    def coordinator_form_values():
        values = {key: request.form.get(key, "").strip() for key in
                  ("city", "name", "phone", "personal_email", "corporate_email", "birth_date")}
        if not values["city"] or len(values["city"]) > 200:
            raise ValueError("Укажите город (до 200 символов).")
        if not values["name"] or len(values["name"]) > 200:
            raise ValueError("Укажите ФИО (до 200 символов).")
        for key, title, limit in (("phone", "Контакт", 200), ("personal_email", "Личная почта", 500),
                                  ("corporate_email", "Корпоративная почта", 2000)):
            if len(values[key]) > limit:
                raise ValueError(f"Поле «{title}» слишком длинное.")
        if values["birth_date"]:
            try:
                date.fromisoformat(values["birth_date"])
            except ValueError as exception:
                raise ValueError("Проверьте дату рождения.") from exception
        return values

    @app.get("/coordinators")
    def coordinators():
        with connection(settings.db_path) as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM coordinators ORDER BY city COLLATE NOCASE,name COLLATE NOCASE")]
        return render_template("coordinators.html", title="Координаторы", coordinators=rows)

    @app.route("/coordinators/new", methods=["GET", "POST"])
    def coordinator_new():
        values = {key: "" for key in ("city", "name", "phone", "personal_email", "corporate_email",
                                      "birth_date")}
        error = None
        if request.method == "POST":
            values = {key: request.form.get(key, "").strip() for key in values}
            try:
                values = coordinator_form_values()
                with connection(settings.db_path) as db:
                    db.execute("""INSERT INTO coordinators(city,name,phone,personal_email,corporate_email,
                        birth_date) VALUES(:city,:name,:phone,:personal_email,:corporate_email,:birth_date)""",
                        values)
                return redirect(url_for("coordinators"))
            except ValueError as exception:
                error = str(exception)
            except Exception as exception:
                if "UNIQUE constraint failed" in str(exception):
                    error = "Координатор с таким именем уже есть в этом городе."
                else:
                    raise
        return render_template("coordinator_form.html", title="Новый координатор", coordinator=values,
                               error=error)

    @app.route("/coordinators/<int:coordinator_id>/edit", methods=["GET", "POST"])
    def coordinator_edit(coordinator_id):
        with connection(settings.db_path) as db:
            current = db.execute("SELECT * FROM coordinators WHERE id=?", [coordinator_id]).fetchone()
        if not current:
            abort(404)
        values = dict(current)
        error = None
        if request.method == "POST":
            values.update({key: request.form.get(key, "").strip() for key in
                           ("city", "name", "phone", "personal_email", "corporate_email", "birth_date")})
            try:
                values = coordinator_form_values()
                with connection(settings.db_path) as db:
                    db.execute("""UPDATE coordinators SET city=:city,name=:name,phone=:phone,
                        personal_email=:personal_email,corporate_email=:corporate_email,
                        birth_date=:birth_date WHERE id=:id""",
                        {**values, "id": coordinator_id})
                return redirect(url_for("coordinators"))
            except ValueError as exception:
                error = str(exception)
            except Exception as exception:
                if "UNIQUE constraint failed" in str(exception):
                    error = "Координатор с таким именем уже есть в этом городе."
                else:
                    raise
        return render_template("coordinator_form.html", title="Редактировать координатора",
                               coordinator=values, error=error)

    def video_form_values():
        values = {key: request.form.get(key, "").strip() for key in
                  ("city", "teacher", "request_date", "sent_date", "module", "video_url",
                   "positive_notes", "growth_notes", "coordinator_id")}
        if not values["city"] or len(values["city"]) > 200:
            raise ValueError("Укажите город (до 200 символов).")
        if not values["teacher"] or len(values["teacher"]) > 200:
            raise ValueError("Укажите преподавателя (до 200 символов).")
        try:
            date.fromisoformat(values["request_date"])
            if values["sent_date"]:
                date.fromisoformat(values["sent_date"])
        except ValueError as exception:
            raise ValueError("Проверьте даты запроса и отправки.") from exception
        if values["module"] not in MODULES:
            raise ValueError("Выберите модуль из списка.")
        if len(values["video_url"]) > 2048:
            raise ValueError("Ссылка слишком длинная.")
        if values["video_url"]:
            parsed = urlsplit(values["video_url"])
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username:
                raise ValueError("Ссылка должна начинаться с http:// или https://.")
        if values["sent_date"] and not values["video_url"]:
            raise ValueError("Добавьте ссылку на отправленное видео.")
        for key, title in (("positive_notes", "Поле «Что хорошо»"),
                            ("growth_notes", "Поле «Зона роста»")):
            if len(values[key]) > 16000:
                raise ValueError(f"{title} не должно превышать 16 000 символов.")
        try:
            values["coordinator_id"] = int(values["coordinator_id"]) if values["coordinator_id"] else None
        except ValueError as exception:
            raise ValueError("Выберите координатора из списка.") from exception
        if values["coordinator_id"] is not None:
            with connection(settings.db_path) as db:
                exists = db.execute("SELECT 1 FROM coordinators WHERE id=?",
                                    [values["coordinator_id"]]).fetchone()
            if not exists:
                raise ValueError("Выбранный координатор не найден.")
        return values

    def video_form_choices(city=""):
        with connection(settings.db_path) as db:
            teachers = sorted({row[0] for row in db.execute(
                "SELECT teacher FROM lessons UNION SELECT teacher FROM videos")}, key=str.casefold)
            cities = sorted({row[0] for row in db.execute(
                "SELECT city FROM lessons UNION SELECT city FROM videos UNION SELECT city FROM coordinators")},
                key=str.casefold)
            coordinator_rows = [dict(row) for row in db.execute(
                "SELECT id,city,name FROM coordinators ORDER BY city COLLATE NOCASE,name COLLATE NOCASE")]
            default_coordinator = db.execute(
                "SELECT id FROM coordinators WHERE city=? COLLATE NOCASE ORDER BY id LIMIT 1", [city]
            ).fetchone() if city else None
        return teachers, cities, coordinator_rows, default_coordinator[0] if default_coordinator else None

    @app.get("/videos")
    def videos():
        city_filter = request.args.get("city", "").strip()
        teacher_filter = request.args.get("teacher", "").strip()
        clauses, params = [], []
        if city_filter:
            clauses.append("v.city=?")
            params.append(city_filter)
        if teacher_filter:
            clauses.append("v.teacher=?")
            params.append(teacher_filter)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with connection(settings.db_path) as db:
            rows = [dict(row) for row in db.execute("""
                SELECT v.*, c.name AS coordinator_name FROM videos v
                LEFT JOIN coordinators c ON c.id=v.coordinator_id
            """ + where + " ORDER BY v.request_date DESC,v.city COLLATE NOCASE,v.teacher COLLATE NOCASE,v.id DESC",
                params)]
        teachers, cities, _, _ = video_form_choices()
        return render_template("videos.html", title="Видео", videos=rows, city_filter=city_filter,
                               teacher_filter=teacher_filter, teachers=teachers, cities=cities)

    def render_video_form(values, error=None, video_id=None):
        teachers, cities, coordinator_rows, default_coordinator = video_form_choices(values.get("city", ""))
        if not values.get("coordinator_id"):
            values["coordinator_id"] = default_coordinator
        return render_template("video_form.html", title="Видео преподавателя" if video_id else "Добавить видео",
                               video=values, teachers=teachers, cities=cities,
                               coordinators=coordinator_rows, error=error, video_id=video_id), 400 if error else 200

    @app.route("/videos/new", methods=["GET", "POST"])
    def video_new():
        values = {key: "" for key in ("city", "teacher", "request_date", "sent_date", "module",
                                      "video_url", "positive_notes", "growth_notes", "coordinator_id")}
        values["request_date"] = utc_now().astimezone(settings.timezone).date().isoformat()
        values["module"] = next(iter(MODULES))
        error = None
        if request.method == "POST":
            values = {key: request.form.get(key, "").strip() for key in values}
            try:
                values = video_form_values()
                now = utc_now().isoformat()
                if values["coordinator_id"] is None:
                    _, _, _, match = video_form_choices(values["city"])
                    values["coordinator_id"] = match
                with connection(settings.db_path) as db:
                    db.execute("""INSERT INTO videos(city,teacher,coordinator_id,request_date,sent_date,
                        module,video_url,positive_notes,growth_notes,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""", [values["city"], values["teacher"],
                        values["coordinator_id"], values["request_date"], values["sent_date"] or None,
                        values["module"], values["video_url"], values["positive_notes"],
                        values["growth_notes"], now, now])
                return redirect(url_for("videos"))
            except ValueError as exception:
                error = str(exception)
        return render_video_form(values, error)

    @app.route("/videos/<int:video_id>/edit", methods=["GET", "POST"])
    def video_edit(video_id):
        with connection(settings.db_path) as db:
            current = db.execute("SELECT * FROM videos WHERE id=?", [video_id]).fetchone()
        if not current:
            abort(404)
        values = dict(current)
        error = None
        if request.method == "POST":
            values.update({key: request.form.get(key, "").strip() for key in
                           ("city", "teacher", "request_date", "sent_date", "module", "video_url",
                            "positive_notes", "growth_notes", "coordinator_id")})
            try:
                values = video_form_values()
                now = utc_now().isoformat()
                if values["coordinator_id"] is None:
                    _, _, _, match = video_form_choices(values["city"])
                    values["coordinator_id"] = match
                with connection(settings.db_path) as db:
                    db.execute("""UPDATE videos SET city=?,teacher=?,coordinator_id=?,request_date=?,
                        sent_date=?,module=?,video_url=?,positive_notes=?,growth_notes=?,updated_at=? WHERE id=?""",
                        [values["city"], values["teacher"], values["coordinator_id"], values["request_date"],
                         values["sent_date"] or None, values["module"], values["video_url"],
                         values["positive_notes"], values["growth_notes"], now, video_id])
                return redirect(url_for("videos"))
            except ValueError as exception:
                error = str(exception)
        return render_video_form(values, error, video_id)

    @app.get("/videos/<int:video_id>/report.xlsx")
    def video_excel_report(video_id):
        with connection(settings.db_path) as db:
            video = db.execute("""
                SELECT v.*, c.name AS coordinator_name FROM videos v
                LEFT JOIN coordinators c ON c.id=v.coordinator_id WHERE v.id=?
            """, [video_id]).fetchone()
        if not video:
            abort(404)
        filename = secure_filename(f"{video['request_date']}-{video['city']}-{video['teacher']}-"
                                   f"{video['module']}.xlsx") or f"video-{video_id}.xlsx"
        return send_file(video_report(dict(video)), as_attachment=True, download_name=filename,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/cities")
    def cities():
        return report("Отчет по городам")

    for name in MODULES:
        slug = name.lower()
        app.add_url_rule(f"/{slug}", slug, lambda module=name: report(module, module))

    @app.get("/weekly")
    @app.get("/weekly/<start_date>")
    def weekly(start_date=None):
        today = utc_now().astimezone(settings.timezone).date()
        value = start_date or request.args.get("week")
        try:
            start = date.fromisoformat(value) if value else today - timedelta(days=today.weekday() + 7)
        except ValueError:
            abort(400, "Invalid week")
        try:
            start -= timedelta(days=start.weekday())
            end = start + timedelta(days=6)
        except OverflowError:
            abort(400, "Invalid week")
        if end < settings.start_date or start > today:
            abort(400, "Week outside the reporting period")
        with connection(settings.db_path) as db:
            rows = db.execute("""SELECT module,topic,city,teacher,group_name,date FROM lessons
                WHERE date >= ? AND date <= ? ORDER BY city,module,date,topic,group_name""",
                [max(start, settings.start_date).isoformat(), end.isoformat()]).fetchall()
        grouped = defaultdict(lambda: defaultdict(list))
        for row in rows:
            grouped[row["city"]][row["module"]].append(dict(row))
        current = today - timedelta(days=today.weekday())
        first = settings.start_date - timedelta(days=settings.start_date.weekday())
        # All weeks since the beginning of the period; no arbitrary 12-week cutoff.
        weeks = []
        cursor = current
        while cursor >= first:
            weeks.append(cursor)
            cursor -= timedelta(days=7)
        calendars = []
        month_keys = sorted({(start.year, start.month), (end.year, end.month)})
        for year, month in month_keys:
            calendars.append({"label": f"{month:02d}.{year}", "month": month,
                              "weeks": calendar.Calendar().monthdatescalendar(year, month)})
        previous = start - timedelta(days=7)
        return render_template("weekly.html", title="Недельный отчет", grouped=grouped,
                               start=start, end=end, total=len(rows), weeks=weeks,
                               previous=previous if previous >= first else None,
                               following=start + timedelta(days=7) if start < current else None,
                               calendars=calendars, today=today, first_week=first,
                               last_week_end=current + timedelta(days=6))

    @app.get("/api/lessons")
    def api_lessons():
        page, size = pagination()
        with connection(settings.db_path) as db:
            rows, total = query_lessons(db, filters(), page, size)
        return jsonify(lessons=rows, pagination={"total_count": total, "current_page": page,
                       "per_page": size, "total_pages": max(1, (total + size - 1) // size)})

    @app.get("/api/status")
    def api_status():
        return jsonify(sync_status(settings))

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def error_page(error):
        if request.path.startswith("/api/"):
            return jsonify(error="Request failed", status=error.code), error.code
        return render_template("error.html", title="Ошибка", code=error.code), error.code

    return app
