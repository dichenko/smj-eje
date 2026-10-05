from dataclasses import dataclass, asdict
from datetime import date, datetime
import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import MODULES


class SourceError(RuntimeError):
    pass


def clean(value):
    return " ".join(value.replace("\xa0", " ").split())


@dataclass(frozen=True)
class Lesson:
    module: str
    topic: str
    city: str
    teacher: str
    group_name: str
    date: str
    stable_id: str | None = None

    def fingerprint(self):
        payload = asdict(self)
        payload.pop("stable_id")
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parse_date(value):
    match = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})\b", value)
    if not match:
        raise SourceError("Missing lesson date; report was not applied")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(match[1], fmt).date().isoformat()
        except ValueError:
            pass
    raise SourceError("Invalid lesson date; report was not applied")


def popover_fields(content):
    soup = BeautifulSoup(content, "html.parser")
    fields = {}
    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) >= 2:
            key = clean(cells[0].get_text(" ", strip=True)).rstrip(":").lower()
            fields[key] = clean(cells[1].get_text(" ", strip=True))
    # Some reports use plain text or <br> rather than a table.
    if not fields:
        for line in soup.get_text("\n", strip=True).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[clean(key).lower()] = clean(value)
    return fields


def parse_report(html, module):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.plan-rep")
    if table is None or not table.find("tr"):
        raise SourceError(f"{module}: report table missing (authentication or layout changed)")
    lessons = []
    for row in table.find_all("tr", recursive=False) or table.select("tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        topic = clean(cells[0].get_text(" ", strip=True))
        for cell in cells[1:]:
            for item in cell.select("div.bull"):
                style = re.sub(r"\s+", "", item.get("style", "")).lower()
                if not re.search(r"(?:^|;)background(?:-color)?:#96fe96(?:;|$)", style):
                    continue
                fields = popover_fields(item.get("data-content", ""))
                date_value = next((v for k, v in fields.items() if "дат" in k), "")
                city = next((v for k, v in fields.items() if "филиал" in k or "город" in k), "")
                teacher = next((v for k, v in fields.items() if "преподаватель" in k or "учитель" in k), "")
                group = next((v for k, v in fields.items() if "групп" in k or "класс" in k), "")
                if not topic or not city or not teacher:
                    raise SourceError(f"{module}: required lesson fields missing; report was not applied")
                stable_id = item.get("data-lesson-id") or item.get("data-event-id")
                lessons.append(Lesson(module, topic, city, teacher, group, parse_date(date_value), stable_id))
    # Do not interpret an incompatible row structure as a successful empty report.
    if table.select("div.bull") and not any(
        row.find_all("td", recursive=False)
        for row in (table.find_all("tr", recursive=False) or table.select("tbody > tr"))
    ):
        raise SourceError(f"{module}: unsupported report structure")
    return lessons, soup


class SmartJSource:
    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "smj-eje/1.0", "Accept-Language": "ru,en;q=0.5"})
        retries = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods={"GET"}, respect_retry_after_header=False)
        self.session.mount("http://", HTTPAdapter(max_retries=retries))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def close(self):
        self.session.close()

    def request(self, method, url, **kwargs):
        # Follow redirects manually so credentials cannot be posted to another host.
        for _ in range(6):
            parsed, base = urlsplit(url), urlsplit(self.settings.source_url)
            if parsed.netloc != base.netloc or parsed.scheme not in {"http", "https"}:
                raise SourceError("Source redirected to an unexpected host")
            if parsed.scheme == "http" and not self.settings.allow_http:
                raise SourceError("Source attempted an insecure HTTP redirect")
            try:
                response = self.session.request(method, url, timeout=(10, 30),
                                                allow_redirects=False, **kwargs)
                response.raise_for_status()
            except requests.RequestException:
                # Never log request bodies, passwords, response HTML, or cookies.
                raise SourceError("Smart-J network/HTTP request failed") from None
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                if response.status_code in {301, 302, 303}:
                    method, kwargs = "GET", {}
                continue
            if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            return response
        raise SourceError("Too many source redirects")

    def collect(self):
        self.settings.validate_source()
        base = self.settings.source_url
        self.request("GET", base)
        response = self.request("POST", base, data={
            "login": self.settings.source_username, "passw": self.settings.source_password,
            "auth_mode": "login",
        })
        if "logout" not in response.text.lower():
            raise SourceError("Smart-J authentication failed")
        result = {}
        for module, number in MODULES.items():
            first = f"{base}{self.settings.report_prefix}/l:{number}/"
            queue, visited, lessons, page_signatures = [first], set(), [], set()
            while queue:
                url = queue.pop(0)
                if url in visited:
                    continue
                if len(visited) >= 100:
                    raise SourceError(f"{module}: pagination limit exceeded")
                visited.add(url)
                response = self.request("GET", url)
                page_lessons, soup = parse_report(response.text, module)
                signature = tuple(sorted((item.stable_id or "", item.fingerprint()) for item in page_lessons))
                if signature and signature in page_signatures:
                    raise SourceError(f"{module}: repeated pagination data; completeness is uncertain")
                page_signatures.add(signature)
                lessons.extend(page_lessons)
                pagination = soup.select(".pagination a[href], a[rel=next][href]")
                for link in pagination:
                    href = link.get("href", "")
                    if not href or href.startswith(("#", "javascript:")):
                        continue
                    next_url = urljoin(url, href)
                    if (urlsplit(next_url).netloc != urlsplit(base).netloc or
                            urlsplit(next_url).path != urlsplit(first).path):
                        raise SourceError(f"{module}: unexpected pagination link")
                    if next_url not in visited and next_url not in queue:
                        queue.append(next_url)
                time.sleep(self.settings.request_pause)
            result[module] = lessons
        return result


def window_start(settings, today):
    from datetime import timedelta
    return max(settings.start_date, today - timedelta(days=settings.mutable_days - 1))


def lesson_date(lesson):
    return date.fromisoformat(lesson.date)
