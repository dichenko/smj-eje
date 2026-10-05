import calendar
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import hmac
import secrets
import time

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import MODULES, Settings
from .db import connection, distinct_values, initialize, query_lessons, sync_status, utc_now


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
        return report("Отчет по преподавателям")

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
