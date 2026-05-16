from __future__ import annotations

import csv
import gzip
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


USER_AGENT = "Mozilla/5.0 (compatible; SitemapDiscovery/1.0; +https://github.com/)"
REQUEST_TIMEOUT = 20
COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
    "/article-sitemap.xml",
    "/news-sitemap.xml",
    "/sitemap1.xml",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def session_with_headers() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def canonical_site(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if not parsed.scheme:
        parsed = urlparse(f"https://{raw_url.strip()}")
    return urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", ""))


def extract_domain(site_url: str) -> str:
    return urlparse(site_url).netloc.lower()


def maybe_decompress(content: bytes, url: str) -> bytes:
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def looks_like_xml_sitemap(content: bytes, url: str) -> bool:
    payload = maybe_decompress(content, url)
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return False
    return local_name(root.tag) in {"urlset", "sitemapindex"}


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if response.status_code >= 400:
            return None
        return response
    except requests.RequestException:
        return None


def extract_from_robots(robots_text: str) -> list[str]:
    matches = []
    for line in robots_text.splitlines():
        match = re.match(r"(?i)\s*sitemap\s*:\s*(\S+)", line.strip())
        if match:
            matches.append(match.group(1).strip())
    return matches


def extract_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for tag in soup.find_all(["a", "link"]):
        href = tag.get("href")
        if not href:
            continue
        lower = href.lower()
        if "sitemap" in lower and lower.startswith("http"):
            candidates.append(href.strip())
    return candidates


def discover_sitemap(session: requests.Session, site_url: str) -> tuple[str, str]:
    site_url = canonical_site(site_url)
    checked: list[str] = []

    robots_url = f"{site_url}/robots.txt"
    robots_response = fetch(session, robots_url)
    if robots_response and robots_response.text:
        for candidate in extract_from_robots(robots_response.text):
            checked.append(candidate)
            response = fetch(session, candidate)
            if response and looks_like_xml_sitemap(response.content, response.url):
                return response.url, "robots.txt"

    for path in COMMON_SITEMAP_PATHS:
        candidate = f"{site_url}{path}"
        checked.append(candidate)
        response = fetch(session, candidate)
        if response and looks_like_xml_sitemap(response.content, response.url):
            return response.url, "common_path"

    homepage = fetch(session, site_url)
    if homepage and "text/html" in homepage.headers.get("content-type", ""):
        for candidate in extract_from_html(homepage.text, site_url):
            checked.append(candidate)
            response = fetch(session, candidate)
            if response and looks_like_xml_sitemap(response.content, response.url):
                return response.url, "homepage_link"

    return "", "; ".join(checked[:10])


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python scripts/discover_sitemaps.py <input_txt> <output_csv>")
        return 1

    input_path = repo_root() / sys.argv[1]
    output_path = repo_root() / sys.argv[2]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    missing_path = output_path.with_name(f"{output_path.stem}_missing.csv")

    raw_sites = [
        line.strip()
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    session = session_with_headers()
    found_rows: list[dict[str, str]] = []
    missing_rows: list[dict[str, str]] = []

    for raw_site in raw_sites:
        site_url = canonical_site(raw_site)
        domain = extract_domain(site_url)
        sitemap_url, source = discover_sitemap(session, site_url)
        if sitemap_url:
            found_rows.append(
                {"site": domain, "homepage_url": site_url, "sitemap_url": sitemap_url}
            )
            print(f"[FOUND] {domain} -> {sitemap_url}")
        else:
            missing_rows.append(
                {
                    "site": domain,
                    "homepage_url": site_url,
                    "checked": source,
                }
            )
            print(f"[MISS] {domain}")

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["site", "homepage_url", "sitemap_url"])
        writer.writeheader()
        writer.writerows(found_rows)

    with missing_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["site", "homepage_url", "checked"])
        writer.writeheader()
        writer.writerows(missing_rows)

    print(f"[INFO] {len(found_rows)} sitemaps trouvés")
    print(f"[INFO] {len(missing_rows)} sitemaps non trouvés")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
