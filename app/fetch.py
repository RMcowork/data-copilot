"""Fetch a data file from a link, safely.

The server makes this request on a user's behalf, so it must not be usable to reach
anything but the public internet: only http(s), no private/loopback/link-local addresses
(checked on every redirect), a hard size cap while streaming, and short timeouts.
Google Sheets share links are turned into their CSV export first.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .config import settings
from .datasets import DATA_EXTENSIONS, DatasetError

MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(20.0, connect=8.0)
# Matches /d/<id> (also behind /u/0/) and the published-to-web form /d/e/<key>.
_SHEETS = re.compile(r"^https://docs\.google\.com/spreadsheets/(?:u/\d+/)?d/(e/)?([\w-]+)")


def normalise_url(url: str) -> str:
    """A Google Sheets link (edit, view, share or published-to-web) becomes the link of its CSV export."""
    url = url.strip()
    m = _SHEETS.match(url)
    if not m or "/export" in url:
        return url
    parts = urlparse(url)
    gid = (parse_qs(parts.fragment).get("gid") or parse_qs(parts.query).get("gid") or [None])[0]
    if m.group(1):  # published to the web: /d/e/<key>/pubhtml or /pub; the key is not a normal sheet id
        if parse_qs(parts.query).get("output") == ["csv"]:
            return url  # already the CSV link Google gave out
        tail = f"&gid={gid}" if gid else ""
        return f"https://docs.google.com/spreadsheets/d/e/{m.group(2)}/pub?output=csv{tail}"
    return f"https://docs.google.com/spreadsheets/d/{m.group(2)}/export?format=csv&gid={gid or 0}"


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified)


def check_public_url(url: str) -> None:
    """Raise DatasetError unless the link is http(s) and every address its host resolves to is public."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DatasetError("The link must start with http:// or https://.")
    if parts.username or parts.password:
        raise DatasetError("Links with a username or password are not accepted.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise DatasetError("Could not find that address. Check the link.") from None
    if not infos or not all(_is_public(i[4][0]) for i in infos):
        raise DatasetError("That address is not on the public internet, so it cannot be fetched.")


def _filename(url: str, content: bytes) -> str:
    name = Path(unquote(urlparse(url).path)).name
    ext = Path(name).suffix.lower()
    if ext in DATA_EXTENSIONS:
        return name
    if "docs.google.com/spreadsheets" in url:
        return "sheet.xlsx" if content[:2] == b"PK" else "sheet.csv"
    return (Path(name).stem or "data") + (".xlsx" if content[:2] == b"PK" else ".csv")


def fetch_url(url: str, *, client: httpx.Client | None = None, check=check_public_url) -> tuple[str, bytes]:
    """Download the file behind a link: (filename, bytes). `client` and `check` exist so tests need no network."""
    limit = settings.max_dataset_mb * 1024 * 1024
    url = normalise_url(url)
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)
    xlsx_url = None  # set when the link is a Sheets CSV export; used if Google has no CSV for it
    if re.search(r"^https://docs\.google\.com/spreadsheets/d/[\w-]+/export\?format=csv", url):
        xlsx_url = re.sub(r"export\?format=csv(&gid=\d+)?", "export?format=xlsx", url)
    try:
        for _ in range(2 * (MAX_REDIRECTS + 1)):
            check(url)
            with client.stream("GET", url, headers={"User-Agent": "DataCopilot/1.0"}) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise DatasetError("The link redirected without saying where.")
                    url = str(httpx.URL(url).join(location))
                    continue
                if resp.status_code in (401, 403):
                    raise DatasetError("The link needs a sign-in. Share it as 'anyone with the link', or upload the file instead.")
                if resp.status_code == 404:
                    raise DatasetError("The link was not found (404).")
                if resp.status_code == 400 and xlsx_url:
                    # An Excel file opened in Google Sheets (link has rtpof=true) has no CSV export, only xlsx.
                    # The 400 can come from the host Google redirected to, so this keys off the original link.
                    url, xlsx_url = xlsx_url, None
                    continue
                if resp.status_code == 400 and "google" in url:
                    raise DatasetError("Google did not accept this Sheets link (400). Copy the link from the sheet's Share button "
                                       "(set to 'anyone with the link'), or use File > Share > Publish to web > CSV. "
                                       "You can also download the sheet as CSV and upload it.")
                if resp.status_code >= 400:
                    raise DatasetError(f"The link answered with an error ({resp.status_code}).")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > limit:
                    raise DatasetError(f"The file is larger than {settings.max_dataset_mb} MB.")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        raise DatasetError(f"The file is larger than {settings.max_dataset_mb} MB.")
                content = bytes(buf)
                head = content[:300].lstrip().lower()
                if head.startswith((b"<!doctype html", b"<html")):
                    raise DatasetError("That link returned a web page, not a data file. For Google Sheets, share the sheet as "
                                       "'anyone with the link' and paste its link; otherwise upload the file.")
                return _filename(url, content), content
        raise DatasetError("The link redirected too many times.")
    except httpx.TimeoutException:
        raise DatasetError("The link took too long to answer.") from None
    except httpx.HTTPError as exc:
        raise DatasetError(f"Could not download the link ({type(exc).__name__}).") from None
    finally:
        if own:
            client.close()
