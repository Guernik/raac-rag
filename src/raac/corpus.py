"""CorpusSource (tracer slice): find one Parte in ANAC's RAAC vigente sheet and download it.

The sheet ID is read from the RAAC page's Poncho config on every run, never hardcoded.
Full row normalization and validation across all Partes is out of this slice.
"""

import csv
import hashlib
import io
import re
import time
from dataclasses import dataclass

import httpx

RAAC_PAGE_URL = "https://www.argentina.gob.ar/anac/raac-dnar-regulaciones-argentinas-de-aviacion-civil/raac"
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv"
_ID_SPREAD_RE = re.compile(r"""["']?idSpread["']?\s*:\s*["']([A-Za-z0-9_-]+)["']""")
_SHARE_LINK_RE = re.compile(r"^https://docs\.anac\.gob\.ar/index\.php/s/[A-Za-z0-9]+$")


class CorpusError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParteListing:
    parte: str  # e.g. "61", "HL"
    titulo: str
    share_url: str  # stable citation URL

    @property
    def download_url(self) -> str:
        return self.share_url + "/download"


@dataclass(frozen=True)
class Pdf:
    data: bytes
    sha256: str


def sheet_id_from_page(html: str) -> str:
    m = _ID_SPREAD_RE.search(html)
    if not m:
        raise CorpusError("RAAC page has no Poncho idSpread config; the source changed shape")
    return m.group(1)


def find_listing(sheet_csv: str, parte: str) -> ParteListing:
    for row in csv.DictReader(io.StringIO(sheet_csv)):
        code = re.sub(r"(?i)^\s*parte\s+", "", (row.get("parte") or "")).strip()
        if code != parte:
            continue
        titulo = re.sub(r"\s+", " ", (row.get("titulo") or "").replace("**", "")).strip()
        link = (row.get("btn-Ver") or "").strip()
        if not _SHARE_LINK_RE.match(link):
            raise CorpusError(f"Parte {parte}: unexpected link {link!r}")
        return ParteListing(parte=code, titulo=titulo, share_url=link)
    raise CorpusError(f"Parte {parte} not listed in the RAAC vigente sheet")


def fetch_listing(parte: str, client: httpx.Client | None = None) -> ParteListing:
    client = client or httpx.Client(follow_redirects=True, timeout=60)
    html = _get(client, RAAC_PAGE_URL).text
    sheet = _get(client, SHEET_CSV_URL.format(id=sheet_id_from_page(html)))
    return find_listing(sheet.content.decode("utf-8"), parte)


def download(listing: ParteListing, client: httpx.Client | None = None) -> Pdf:
    client = client or httpx.Client(follow_redirects=True, timeout=120)
    data = _get(client, listing.download_url).content
    if not data.startswith(b"%PDF"):
        raise CorpusError(f"Parte {listing.parte}: download is not a PDF")
    return Pdf(data=data, sha256=hashlib.sha256(data).hexdigest())


def _get(client: httpx.Client, url: str, attempts: int = 4) -> httpx.Response:
    # docs.anac.gob.ar resets connections intermittently; retry transport errors only.
    for attempt in range(attempts):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")
