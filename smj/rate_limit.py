import hashlib
import os
import re

from flask import make_response, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits.errors import StorageError

LOGIN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}")
PRODUCTION_STORAGE = "redis://127.0.0.1:6379/4"


def normalize_login(value):
    value = value.strip()
    return value.lower() if value.isascii() else value


def valid_login(normalized):
    return LOGIN_PATTERN.fullmatch(normalized) is not None


def login_limiter_healthy(app):
    return app.extensions["smj_login_limiter"].storage.check()


def init_login_limiter(app):
    production = app.config["SETTINGS"].production
    uri = (app.config.get("LOGIN_RATE_LIMIT_STORAGE_URI")
           or os.environ.get("LOGIN_RATE_LIMIT_STORAGE_URI")
           or (PRODUCTION_STORAGE if production else "memory://"))
    redis = uri.startswith(("redis://", "rediss://", "redis+unix://"))
    if production and not redis and not (app.testing and uri == "memory://"):
        raise ValueError("Production login rate limiting requires Redis")

    limiter = Limiter(
        get_remote_address,
        storage_uri=uri,
        storage_options={"wrap_exceptions": True, "socket_connect_timeout": 1,
                         "socket_timeout": 1, "retry_on_timeout": False} if redis else {},
        strategy="sliding-window-counter",
        headers_enabled=True,
        retry_after="delta-seconds",
        swallow_errors=False,
        in_memory_fallback_enabled=False,
    )

    def login_key():
        login = normalize_login(request.form.get("login", ""))
        # Bound key length and apply the same policy to every submitted login.
        return hashlib.sha256(login.encode()).hexdigest()

    def too_many_attempts(_limit):
        return make_response("Слишком много попыток входа. Повторите позже.", 429)

    def unavailable(error):
        app.logger.warning("Login rate limit storage is unavailable: %s", type(error.__cause__).__name__)
        return "Вход временно недоступен. Повторите позже.", 503, {"Retry-After": "30"}

    limiter.init_app(app)
    app.extensions["smj_login_limiter"] = limiter
    app.register_error_handler(StorageError, unavailable)
    view = app.view_functions["login"]
    for value, key, scope in (
        (app.config.get("LOGIN_RATE_LIMIT_PER_IP", "10/minute;40/15 minutes"),
         get_remote_address, "ip"),
        (app.config.get("LOGIN_RATE_LIMIT_PER_LOGIN", "30/15 minutes"), login_key, "login"),
    ):
        view = limiter.limit(value, key_func=key, scope=scope, methods=["POST"],
                             on_breach=too_many_attempts)(view)
    app.view_functions["login"] = view
