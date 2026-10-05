import pytest

from smj.source import SmartJSource, SourceError, parse_report, parse_date


def report(content, style="background: #96fe96;", extra=""):
    return f'''<table class="plan-rep"><tr><td>Тема</td><td>Город</td></tr>
    <tr><td>Тема 1</td><td><div class="bull" style="{style}" {extra}
    data-content='{content}'></div></td></tr></table>'''


CONTENT = ("<table><tr><td>Дата:</td><td><b>1.10.2026</b></td></tr>"
           "<tr><td>Группа:</td><td>Группа 1</td></tr>"
           "<tr><td>Филиал:</td><td>Москва</td></tr>"
           "<tr><td>Преподаватель:</td><td>Учитель 1</td></tr></table>")


def test_real_report_layout_and_optional_stable_id():
    lessons, _ = parse_report(report(CONTENT, extra='data-lesson-id="42"'), "Kids")
    assert len(lessons) == 1
    assert lessons[0].date == "2026-10-01"
    assert lessons[0].teacher == "Учитель 1"
    assert lessons[0].stable_id == "42"


def test_tbody_structure():
    html = report(CONTENT).replace('<table class="plan-rep">', '<table class="plan-rep"><tbody>')
    html = html.replace("</div></td></tr></table>", "</div></td></tr></tbody></table>")
    assert len(parse_report(html, "Kids")[0]) == 1


def test_only_performed_green_lessons():
    assert parse_report(report(CONTENT, style="background:#eeeeee;"), "Kids")[0] == []


def test_missing_table_is_failure_not_empty_success():
    with pytest.raises(SourceError, match="table missing"):
        parse_report("<form>login</form>", "Kids")


def test_invalid_green_cell_aborts_entire_report():
    with pytest.raises(SourceError):
        parse_report(report("<table></table>"), "Kids")


@pytest.mark.parametrize("value,expected", [("02.09.2026", "2026-09-02"),
    ("2026-09-01", "2026-09-01"), ("Дата: 1.9.2026", "2026-09-01")])
def test_dates(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("value", ["неизвестно", "32.09.2026", "2026-99-99"])
def test_invalid_dates(value):
    with pytest.raises(SourceError):
        parse_date(value)


def test_collector_requires_all_four_reports(settings, monkeypatch):
    source = SmartJSource(settings)
    class Response:
        text = "logout"
    def request(method, url, **kwargs):
        response = Response()
        if "/l:" in url:
            response.text = report(CONTENT) if "/l:56/" not in url else "broken report"
        return response
    monkeypatch.setattr(source, "request", request)
    with pytest.raises(SourceError, match="Junior"):
        source.collect()
    source.close()


def test_collector_login_failure(settings, monkeypatch):
    source = SmartJSource(settings)
    class Response:
        text = "Login failed"
    monkeypatch.setattr(source, "request", lambda *a, **k: Response())
    with pytest.raises(SourceError, match="authentication"):
        source.collect()
    source.close()


@pytest.mark.parametrize("location", ["https://evil.test/", "http://example.test/"])
def test_redirect_cannot_leak_credentials_or_downgrade_tls(settings, monkeypatch, location):
    import requests
    source = SmartJSource(settings)
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        response = requests.Response()
        response.status_code = 307
        response.headers["Location"] = location
        return response
    monkeypatch.setattr(source.session, "request", request)
    with pytest.raises(SourceError):
        source.request("POST", settings.source_url, data={"passw": "synthetic-test-password"})
    assert len(calls) == 1
    source.close()
