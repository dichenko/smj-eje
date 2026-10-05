import os
from pathlib import Path
import socket
import subprocess
import time

import pytest
import requests


@pytest.mark.skipif(os.name == "nt", reason="Gunicorn is the Linux production server")
def test_real_gunicorn_startup_and_http(settings):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "ENV_FILE": str(settings.db_path.parent / "absent.env"),
           "DB_PATH": str(settings.db_path), "APP_PASSWORD": settings.password,
           "FLASK_SECRET_KEY": settings.secret_key, "APP_ENV": "production",
           "SMARTJ_BASE_URL": "https://example.test"}
    process = subprocess.Popen(
        ["gunicorn", "--bind", f"127.0.0.1:{port}", "--workers", "1", "--threads", "2", "wsgi:app"],
        cwd=Path(__file__).resolve().parent.parent, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, "Gunicorn exited during startup"
            try:
                response = requests.get(url + "/healthz", timeout=1)
                if response.status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            assert time.monotonic() < deadline, "Gunicorn did not become ready"
            time.sleep(0.1)
        assert response.json() == {"status": "ok"}
        assert requests.get(url + "/api/lessons", timeout=2).status_code == 401
        assert requests.get(url + "/login", timeout=2).status_code == 200
        assert requests.get(url + "/weekly", allow_redirects=False, timeout=2).status_code == 302
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
