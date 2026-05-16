from __future__ import annotations

import csv
import gzip
import json
import os
import re
import smtplib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from keybert import KeyBERT


USER_AGENT = (
    "Mozilla/5.0 (compatible; SitemapTopicMonitor/2.0; +https://github.com/)"
)
REQUEST_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
EMAIL_SUBJECT_PREFIX = os.getenv("EMAIL_SUBJECT_PREFIX", "Veille sitemaps")
MAX_CHILD_SITEMAPS = int(os.getenv("MAX_CHILD_SITEMAPS", "200"))
KEYBERT_MODEL = os.getenv("KEYBERT_MODEL", "all-MiniLM-L6-v2")
BOOTSTRAP_ONLY = os.getenv("BOOTSTRAP_ONLY", "false").lower() == "true"

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


def read_theme_sources(base_dir: Path) -> list[SitemapSource]:
    theme_dir = base_dir / "data" / "themes"
    ensure_dir(theme_dir)
    sources: list[SitemapSource] = []

    for csv_path in sorted(theme_dir.glob("*.csv")):
        theme = csv_path.stem
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(2048)
            handle.seek(0)
            try:
                has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
            except csv.Error:
                has_header = False

            if has_header:
                reader = csv.DictReader(handle)
                for row in reader:
                    site = (row.get("site") or row.get("Site") or "").strip()
                    sitemap_url = (
                        row.get("sitemap_url")
                        or row.get("sitemap")
                        or row.get("Sitemap")
                        or ""
                    ).strip()
                    if site and sitemap_url:
                        sources.append(SitemapSource(theme, site, sitemap_url))
            else:
                reader = csv.reader(handle)
                for row in reader:
                    if len(row) < 2:
                        continue
                    site = row[0].strip()
                    sitemap_url = row[1].strip()
                    if site and sitemap_url:
                        sources.append(SitemapSource(theme, site, sitemap_url))

    return sources


def fetch_bytes(session: requests.Session, url: str) -> bytes:
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.content


def maybe_decompress(content: bytes, url: str) -> bytes:
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def parse_sitemap(session: requests.Session, sitemap_url: str, depth: int = 0) -> set[str]:
    if depth > 3:
        return set()

    try:
        raw_bytes = fetch_bytes(session, sitemap_url)
    except requests.RequestException as exc:
        print(f"[WARN] sitemap inaccessible: {sitemap_url} ({exc})")
        return set()

    xml_bytes = maybe_decompress(raw_bytes, sitemap_url)

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        print(f"[WARN] XML invalide: {sitemap_url} ({exc})")
        return set()

    root_name = local_name(root.tag)
    if root_name == "urlset":
        urls = set()
        for url_node in root:
            if local_name(url_node.tag) != "url":
                continue
            for child in url_node:
                if local_name(child.tag) == "loc" and child.text:
                    urls.add(child.text.strip())
        return urls

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
        for child_url in child_sitemaps[:MAX_CHILD_SITEMAPS]:
            urls.update(parse_sitemap(session, child_url, depth + 1))
        return urls

    print(f"[WARN] format XML non géré: {sitemap_url}")
    return set()


def snapshot_path(base_dir: Path, theme: str, site: str) -> Path:
    return base_dir / "state" / "snapshots" / theme / f"{slugify(site)}.json"


def load_snapshot(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return set(payload.get("urls", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_snapshot(path: Path, sitemap_url: str, urls: Iterable[str]) -> None:
    ensure_dir(path.parent)
    payload = {
        "sitemap_url": sitemap_url,
        "updated_on": date.today().isoformat(),
        "urls": sorted(set(urls)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_page_title(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    if soup.title and soup.title.string:
        return re.sub(r"\s+", " ", soup.title.string).strip()
    return ""


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
        print("[INFO] SMTP non configuré, email ignoré.")
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

    print(f"[INFO] Email envoyé à {config['EMAIL_TO']}")


def process() -> int:
    base_dir = repo_root()
    today = date.today().isoformat()
    sources = read_theme_sources(base_dir)

    if not sources:
        print("[INFO] Aucun CSV trouvé dans data/themes.")
        return 0

    session = session_with_headers()
    pending_urls: list[tuple[SitemapSource, str]] = []

    for source in sources:
        print(f"[INFO] Traitement {source.theme} / {source.site}")
        current_urls = parse_sitemap(session, source.sitemap_url)
        if not current_urls:
            print(f"[INFO] Aucun URL récupéré pour {source.site}")
            continue

        snapshot_file = snapshot_path(base_dir, source.theme, source.site)
        previous_urls = load_snapshot(snapshot_file)
        new_urls = sorted(current_urls - previous_urls)
        save_snapshot(snapshot_file, source.sitemap_url, current_urls)

        if BOOTSTRAP_ONLY:
            continue

        if not new_urls:
            print(f"[INFO] Pas de nouvelles URLs pour {source.site}")
            continue

        for url in new_urls:
            pending_urls.append((source, url))

    if BOOTSTRAP_ONLY:
        print("[INFO] Mode bootstrap terminé. Snapshots initialisés sans reporting.")
        return 0

    if not pending_urls:
        print("[INFO] Aucune nouvelle URL à notifier.")
        return 0

    print(f"[INFO] Initialisation KeyBERT ({KEYBERT_MODEL})")
    extractor = KeyBERT(model=KEYBERT_MODEL)
    new_records_by_theme: dict[str, list[UrlRecord]] = defaultdict(list)

    for source, url in pending_urls:
        title = fetch_page_title(session, url)
        keyword = extract_keyword_from_title(extractor, title) if title else ""
        new_records_by_theme[source.theme].append(
            UrlRecord(
                detected_on=today,
                theme=source.theme,
                domain=source.site,
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
