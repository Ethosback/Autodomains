from __future__ import annotations

import csv
import gzip
import json
import os
import re
import smtplib
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from keybert import KeyBERT


USER_AGENT = (
    "Mozilla/5.0 (compatible; SitemapTopicMonitor/3.0; +https://github.com/)"
)
SITEMAP_TIMEOUT = int(os.getenv("SITEMAP_TIMEOUT", "30"))
TITLE_TIMEOUT = int(os.getenv("TITLE_TIMEOUT", "10"))
TITLE_BYTE_LIMIT = int(os.getenv("TITLE_BYTE_LIMIT", "131072"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
EMAIL_SUBJECT_PREFIX = os.getenv("EMAIL_SUBJECT_PREFIX", "Veille sitemaps")
KEYBERT_MODEL = os.getenv("KEYBERT_MODEL", "all-MiniLM-L6-v2")
BOOTSTRAP_ONLY = os.getenv("BOOTSTRAP_ONLY", "false").lower() == "true"
TITLE_WORKERS = int(os.getenv("TITLE_WORKERS", "12"))
PROGRESS_EVERY = int(os.getenv("PROGRESS_EVERY", "25"))

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

STOPWORDS = {
    "a",
    "an",
    "and",
    "au",
    "aux",
    "avec",
    "ce",
    "ces",
    "comment",
    "dans",
    "de",
    "des",
    "du",
    "en",
    "et",
    "for",
    "how",
    "guide",
    "bien",
    "complet",
    "complete",
    "definition",
    "la",
    "le",
    "les",
    "mais",
    "ou",
    "par",
    "pour",
    "sur",
    "the",
    "to",
    "un",
    "une",
    "vs",
}


@dataclass
class SitemapSource:
    theme: str
    site: str
    sitemap_url: str


@dataclass
class UrlRecord:
    detected_on: str
    theme: str
    domain: str
    url: str
    title: str
    keyword_keybert: str


def log_info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


def log_warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def session_with_headers() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def normalize_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        return ""

    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"

    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, parts.query, ""))


def read_theme_sources(base_dir: Path) -> list[SitemapSource]:
    theme_dir = base_dir / "data" / "themes"
    ensure_dir(theme_dir)
    sources: list[SitemapSource] = []

    for csv_path in sorted(theme_dir.glob("*.csv")):
        if csv_path.stem.endswith("_missing"):
            continue

        theme = csv_path.stem
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))

        if not rows:
            continue

        first_row_lower = [cell.strip().lower() for cell in rows[0]]
        has_header = "site" in first_row_lower and (
            "sitemap_url" in first_row_lower or "sitemap" in first_row_lower
        )

        if has_header:
            header = first_row_lower
            for row in rows[1:]:
                if not row:
                    continue
                padded = row + [""] * max(0, len(header) - len(row))
                row_map = {header[i]: padded[i].strip() for i in range(len(header))}
                site = row_map.get("site", "").strip()
                sitemap_url = (
                    row_map.get("sitemap_url", "")
                    or row_map.get("sitemap", "")
                    or row_map.get("sitemapurl", "")
                ).strip()
                if site and sitemap_url:
                    sources.append(SitemapSource(theme, site, sitemap_url))
        else:
            for row in rows:
                cleaned = [cell.strip() for cell in row]
                if len(cleaned) >= 3:
                    site = cleaned[0]
                    sitemap_url = cleaned[2]
                elif len(cleaned) >= 2:
                    site = cleaned[0]
                    sitemap_url = cleaned[1]
                else:
                    continue
                if site and sitemap_url:
                    sources.append(SitemapSource(theme, site, sitemap_url))

    return sources


def fetch_response(
    session: requests.Session,
    url: str,
    timeout: int,
    stream: bool = False,
) -> requests.Response:
    response = session.get(url, timeout=timeout, allow_redirects=True, stream=stream)
    if response.status_code >= 400:
        response.raise_for_status()
    return response


def get_with_retries(
    session: requests.Session,
    url: str,
    timeout: int,
    stream: bool = False,
) -> requests.Response:
    last_error: Exception | None = None

    for attempt in range(1, HTTP_RETRIES + 1):
        response: requests.Response | None = None
        try:
            response = fetch_response(session, url, timeout=timeout, stream=stream)
            return response
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            last_error = exc
            if response is not None:
                response.close()
            if status_code not in RETRYABLE_STATUS_CODES or attempt == HTTP_RETRIES:
                raise
        except requests.RequestException as exc:
            last_error = exc
            if attempt == HTTP_RETRIES:
                raise

        time.sleep(min(attempt, 3))

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"unexpected retry state for {url}")


def maybe_decompress(content: bytes, url: str) -> bytes:
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def parse_sitemap(
    session: requests.Session,
    sitemap_url: str,
    visited: set[str] | None = None,
) -> tuple[set[str], bool]:
    normalized_sitemap_url = normalize_url(sitemap_url)
    if visited is None:
        visited = set()

    if normalized_sitemap_url in visited:
        return set(), True
    visited.add(normalized_sitemap_url)

    try:
        response = get_with_retries(session, normalized_sitemap_url, SITEMAP_TIMEOUT)
        raw_bytes = response.content
        final_url = response.url
    except requests.RequestException as exc:
        log_warn(f"sitemap inaccessible: {normalized_sitemap_url} ({exc})")
        return set(), False

    xml_bytes = maybe_decompress(raw_bytes, final_url)

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log_warn(f"XML invalide: {normalized_sitemap_url} ({exc})")
        return set(), False

    root_name = local_name(root.tag)
    if root_name == "urlset":
        urls = set()
        for url_node in root:
            if local_name(url_node.tag) != "url":
                continue
            for child in url_node:
                if local_name(child.tag) == "loc" and child.text:
                    normalized_url = normalize_url(child.text)
                    if normalized_url:
                        urls.add(normalized_url)
        return urls, True

    if root_name == "sitemapindex":
        child_sitemaps: list[str] = []
        for sitemap_node in root:
            if local_name(sitemap_node.tag) != "sitemap":
                continue
            for child in sitemap_node:
                if local_name(child.tag) == "loc" and child.text:
                    child_sitemaps.append(child.text.strip())
                    break

        urls: set[str] = set()
        all_ok = True
        for child_url in child_sitemaps:
            child_urls, child_ok = parse_sitemap(session, child_url, visited)
            urls.update(child_urls)
            if not child_ok:
                all_ok = False
        return urls, all_ok

    log_warn(f"format XML non géré: {normalized_sitemap_url}")
    return set(), False


def snapshot_path(base_dir: Path, theme: str, site: str) -> Path:
    return base_dir / "state" / "snapshots" / theme / f"{slugify(site)}.json"


def ever_seen_path(base_dir: Path, theme: str, site: str) -> Path:
    return base_dir / "state" / "ever_seen" / theme / f"{slugify(site)}.json"


def load_url_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return set(payload.get("urls", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_url_set(
    path: Path,
    urls: Iterable[str],
    sitemap_urls: Iterable[str] | None = None,
) -> None:
    ensure_dir(path.parent)
    payload: dict[str, object] = {
        "updated_on": date.today().isoformat(),
        "urls": sorted(set(urls)),
    }
    if sitemap_urls is not None:
        payload["sitemap_urls"] = sorted(set(sitemap_urls))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_page_title(url: str) -> str:
    session = session_with_headers()
    response: requests.Response | None = None

    try:
        response = get_with_retries(session, url, TITLE_TIMEOUT, stream=True)
        chunks: list[bytes] = []
        total = 0
        title_found = False

        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            joined_lower = b"".join(chunks).lower()
            if b"</title" in joined_lower:
                title_found = True
                break
            if total >= TITLE_BYTE_LIMIT:
                break

        raw_html = b"".join(chunks)
        if not raw_html:
            return ""

        encoding = response.encoding or response.apparent_encoding or "utf-8"
        html = raw_html.decode(encoding, errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            return re.sub(r"\s+", " ", soup.title.string).strip()
        if title_found:
            return ""
        return ""
    except requests.RequestException:
        return ""
    finally:
        if response is not None:
            response.close()
        session.close()


def focus_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip()
    if not cleaned:
        return ""

    parts = [
        part.strip()
        for part in re.split(r"\s(?:\||-|–|:|»)\s", cleaned)
        if part.strip()
    ]
    if not parts:
        return cleaned

    return parts[0]


def extract_keyword_from_title(extractor: KeyBERT, title: str) -> str:
    base_text = focus_title(title)
    if not base_text:
        return ""

    for ngram_range in ((2, 3), (1, 2), (1, 1)):
        keywords = extractor.extract_keywords(
            base_text,
            keyphrase_ngram_range=ngram_range,
            stop_words=list(STOPWORDS),
            top_n=5,
        )
        for phrase, _score in keywords:
            phrase = phrase.strip()
            if phrase:
                return phrase
    return ""


def append_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    ensure_dir(path.parent)
    file_exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def write_daily_report(path: Path, records: list[UrlRecord]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["domain", "title", "keyword_keybert", "url", "detected_on"],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "domain": record.domain,
                    "title": record.title,
                    "keyword_keybert": record.keyword_keybert,
                    "url": record.url,
                    "detected_on": record.detected_on,
                }
            )


def history_rows(records: list[UrlRecord]) -> list[dict[str, str]]:
    return [
        {
            "detected_on": record.detected_on,
            "domain": record.domain,
            "url": record.url,
            "title": record.title,
            "keyword_keybert": record.keyword_keybert,
        }
        for record in records
    ]


def smtp_config() -> dict[str, str]:
    keys = [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "EMAIL_FROM",
        "EMAIL_TO",
    ]
    values = {key: os.getenv(key, "").strip() for key in keys}
    if all(values.values()):
        return values
    return {}


def send_email(report_paths: list[Path], summary: dict[str, int]) -> None:
    config = smtp_config()
    if not config:
        log_info("SMTP non configuré, email ignoré.")
        return

    subject = f"{EMAIL_SUBJECT_PREFIX} - {date.today().isoformat()}"
    body_lines = ["Nouvelles URLs détectées par thématique :", ""]
    for theme, count in sorted(summary.items()):
        body_lines.append(f"- {theme}: {count}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["EMAIL_FROM"]
    message["To"] = config["EMAIL_TO"]
    message.set_content("\n".join(body_lines))

    for report_path in report_paths:
        message.add_attachment(
            report_path.read_bytes(),
            maintype="text",
            subtype="csv",
            filename=report_path.name,
        )

    port = int(config["SMTP_PORT"])
    with smtplib.SMTP(config["SMTP_HOST"], port) as server:
        server.starttls()
        server.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
        server.send_message(message)

    log_info(f"Email envoyé à {config['EMAIL_TO']}")


def process() -> int:
    base_dir = repo_root()
    today = date.today().isoformat()
    sources = read_theme_sources(base_dir)

    if not sources:
        log_info("Aucun CSV trouvé dans data/themes.")
        return 0

    grouped_sources: dict[tuple[str, str], list[str]] = defaultdict(list)
    for source in sources:
        grouped_sources[(source.theme, source.site)].append(source.sitemap_url)

    session = session_with_headers()
    pending_urls: list[tuple[str, str, str]] = []
    total_sites = len(grouped_sources)

    for index, ((theme, site), sitemap_urls) in enumerate(sorted(grouped_sources.items()), start=1):
        log_info(f"[{index}/{total_sites}] Traitement {theme} / {site}")
        current_urls: set[str] = set()
        crawl_complete = True

        for sitemap_url in sitemap_urls:
            parsed_urls, parsed_ok = parse_sitemap(session, sitemap_url)
            current_urls.update(parsed_urls)
            if not parsed_ok:
                crawl_complete = False

        if not crawl_complete:
            log_warn(f"Crawl sitemap incomplet pour {site}, snapshot conservé.")
            continue

        if not current_urls:
            log_info(f"Aucune URL récupérée pour {site}, snapshot conservé.")
            continue

        snapshot_file = snapshot_path(base_dir, theme, site)
        ever_seen_file = ever_seen_path(base_dir, theme, site)
        previous_urls = load_url_set(snapshot_file)
        ever_seen_urls = load_url_set(ever_seen_file)

        new_urls = sorted(url for url in current_urls - previous_urls if url not in ever_seen_urls)

        save_url_set(snapshot_file, current_urls, sitemap_urls=sitemap_urls)
        updated_ever_seen = set(ever_seen_urls)
        updated_ever_seen.update(current_urls)
        save_url_set(ever_seen_file, updated_ever_seen)

        if BOOTSTRAP_ONLY:
            continue

        if not new_urls:
            log_info(f"Pas de nouvelles URLs pour {site}")
            continue

        log_info(f"{site}: {len(new_urls)} nouvelle(s) URL(s)")
        for url in new_urls:
            pending_urls.append((theme, site, url))

    session.close()

    if BOOTSTRAP_ONLY:
        log_info("Mode bootstrap terminé. Snapshots et ever_seen initialisés sans reporting.")
        return 0

    if not pending_urls:
        log_info("Aucune nouvelle URL à notifier.")
        return 0

    log_info(f"{len(pending_urls)} nouvelle(s) URL(s) à enrichir avec le <title>")
    title_results: dict[str, str] = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=max(1, TITLE_WORKERS)) as executor:
        future_to_url = {
            executor.submit(fetch_page_title, url): url
            for _theme, _site, url in pending_urls
        }
        total_titles = len(future_to_url)
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                title_results[url] = future.result()
            except Exception as exc:  # pragma: no cover - guardrail
                log_warn(f"échec inattendu sur le title {url} ({exc})")
                title_results[url] = ""
            completed += 1
            if completed % PROGRESS_EVERY == 0 or completed == total_titles:
                log_info(f"Titles récupérés: {completed}/{total_titles}")

    log_info(f"Initialisation KeyBERT ({KEYBERT_MODEL})")
    extractor = KeyBERT(model=KEYBERT_MODEL)
    new_records_by_theme: dict[str, list[UrlRecord]] = defaultdict(list)

    for theme, site, url in pending_urls:
        title = title_results.get(url, "")
        keyword = extract_keyword_from_title(extractor, title) if title else ""
        new_records_by_theme[theme].append(
            UrlRecord(
                detected_on=today,
                theme=theme,
                domain=site,
                url=url,
                title=title,
                keyword_keybert=keyword,
            )
        )

    daily_report_paths: list[Path] = []
    email_summary: dict[str, int] = {}

    for theme, records in sorted(new_records_by_theme.items()):
        if not records:
            continue

        history_path = base_dir / "reports" / "history" / f"{theme}_all_urls.csv"
        append_csv_rows(
            history_path,
            ["detected_on", "domain", "url", "title", "keyword_keybert"],
            history_rows(records),
        )

        daily_report_path = base_dir / "reports" / "daily" / today / f"{theme}_new_urls.csv"
        write_daily_report(daily_report_path, records)
        daily_report_paths.append(daily_report_path)
        email_summary[theme] = len(records)

    send_email(daily_report_paths, email_summary)
    return 0


if __name__ == "__main__":
    sys.exit(process())
