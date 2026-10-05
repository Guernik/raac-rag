"""CorpusSource: discover the RAAC vigente from ANAC's own page and download every Parte.

The sheet ID is read from the RAAC page's Poncho config on every run, never hardcoded.
The sheet is hand-maintained, so every row is normalized and anything unexpected raises.
"""

import csv
import hashlib
import io
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import strings

RAAC_PAGE_URL = "https://www.argentina.gob.ar/anac/raac-dnar-regulaciones-argentinas-de-aviacion-civil/raac"
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv"
SHEET_COLUMNS = ["parte", "titulo", "btn-Ver", "orden"]
_ID_SPREAD_RE = re.compile(r"""["']?idSpread["']?\s*:\s*["']([A-Za-z0-9_-]+)["']""")
ANAC_HOST = "https://docs.anac.gob.ar"
_SHARE_LINK_RE = re.compile(r"https://docs\.anac\.gob\.ar/index\.php/s/([A-Za-z0-9]+)(?:\?(\S+))?")
_WEBDAV_PROPFIND = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    "<d:prop><oc:fileid/></d:prop></d:propfind>"
)
_PARTE_RE = re.compile(r"Parte\s+([0-9]+|[A-Z]+)")


class CorpusError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParteListing:
    parte: str  # e.g. "61", "HL"
    titulo: str
    share_url: str  # stable citation URL, as published by ANAC
    # Most links share the PDF itself. A few (e.g. Parte 77) share a whole folder and point at
    # the PDF with "?path=<folder>&openfile=<fileid>"; those carry the folder and file id.
    folder: str | None = None
    file_id: str | None = None

    @property
    def token(self) -> str:
        return _SHARE_LINK_RE.fullmatch(self.share_url).group(1)

    @property
    def download_url(self) -> str:
        if self.file_id:
            raise CorpusError(f"Parte {self.parte}: folder share link; the PDF is resolved at download time")
        return self.share_url + "/download"


@dataclass(frozen=True)
class Pdf:
    data: bytes
    sha256: str


@dataclass(frozen=True)
class SourcedPdf:
    listing: ParteListing
    pdf: Pdf
    from_cache: bool  # ANAC was unreachable (or the run was offline), so the newest stored PDF was used


def sheet_id_from_page(html: str) -> str:
    m = _ID_SPREAD_RE.search(html)
    if not m:
        raise CorpusError("RAAC page has no Poncho idSpread config; the source changed shape")
    return m.group(1)


def parse_sheet(sheet_csv: str) -> list[ParteListing]:
    """Normalize every row of the RAAC vigente sheet; raise on anything unexpected."""
    rows = list(csv.reader(io.StringIO(sheet_csv)))
    if not rows or [c.strip() for c in rows[0]] != SHEET_COLUMNS:
        raise CorpusError(f"RAAC sheet header is {rows[0] if rows else None!r}, expected {SHEET_COLUMNS!r}")
    listings: list[ParteListing] = []
    seen: set[str] = set()
    for line, row in enumerate(rows[1:], start=2):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if len(cells) != len(SHEET_COLUMNS):
            raise CorpusError(f"RAAC sheet row {line}: expected {len(SHEET_COLUMNS)} columns, got {row!r}")
        raw_parte, raw_titulo, raw_link, _orden = cells  # orden is meaningless
        if raw_parte.lower() == "parte" and raw_titulo.lower() in ("título", "titulo"):
            continue  # the sheet repeats its headers in Spanish
        m = _PARTE_RE.fullmatch(raw_parte)
        if not m:
            raise CorpusError(f"RAAC sheet row {line}: unexpected Parte {raw_parte!r}")
        code = m.group(1)
        if code in seen:
            raise CorpusError(f"RAAC sheet row {line}: Parte {code} listed twice")
        titulo = re.sub(r"\s+", " ", raw_titulo.replace("**", "")).strip()
        if not titulo:
            raise CorpusError(f"RAAC sheet row {line}: Parte {code} has no titulo")
        if not raw_link:
            raise CorpusError(f"RAAC sheet row {line}: Parte {code} has no link")
        link = _SHARE_LINK_RE.fullmatch(raw_link)
        if not link:
            raise CorpusError(f"RAAC sheet row {line}: Parte {code} has unexpected link {raw_link!r}")
        folder = file_id = None
        if link.group(2):
            query = urllib.parse.parse_qs(link.group(2))
            folder = (query.get("path") or [""])[0]
            file_id = (query.get("openfile") or [""])[0]
            if not folder.startswith("/") or not file_id.isdigit():
                raise CorpusError(f"RAAC sheet row {line}: Parte {code} has unexpected link {raw_link!r}")
        seen.add(code)
        listings.append(ParteListing(parte=code, titulo=titulo, share_url=raw_link, folder=folder, file_id=file_id))
    if not listings:
        raise CorpusError("RAAC sheet lists no Partes")
    return listings


def find_listing(listings: list[ParteListing], parte: str) -> ParteListing:
    for listing in listings:
        if listing.parte == parte:
            return listing
    raise CorpusError(f"Parte {parte} not listed in the RAAC vigente sheet")


def list_partes(client: httpx.Client | None = None) -> list[ParteListing]:
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=10))
    html = _get(client, RAAC_PAGE_URL).text
    sheet = _get(client, SHEET_CSV_URL.format(id=sheet_id_from_page(html)))
    return parse_sheet(sheet.content.decode("utf-8"))


def fetch_listings(partes: list[str], client: httpx.Client | None = None) -> list[ParteListing]:
    """Read the sheet once and find each requested Parte in it."""
    listings = list_partes(client)
    return [find_listing(listings, parte) for parte in partes]


def download(listing: ParteListing, client: httpx.Client | None = None) -> Pdf:
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120, connect=10))
    if listing.file_id:
        data = _get(client, _resolve_folder_file(listing, client), auth=(listing.token, "")).content
    else:
        data = _get(client, listing.download_url).content
    if not data.startswith(b"%PDF"):
        raise CorpusError(f"Parte {listing.parte}: download is not a PDF")
    return Pdf(data=data, sha256=hashlib.sha256(data).hexdigest())


class CorpusStore:
    """Downloaded PDFs on disk, one file per Parte and content hash, plus a manifest.

    Layout: `<root>/<parte>/<sha256>.pdf` and `<root>/manifest.json` with the latest
    listing and hash per Parte. Changes are detected by content hash only, since
    ANAC share links carry no version.
    """

    NEW, CHANGED, UNCHANGED = "new", "changed", "unchanged"

    def __init__(self, root: Path):
        self.root = root
        self._manifest_path = root / "manifest.json"

    def manifest(self) -> dict[str, dict]:
        if not self._manifest_path.exists():
            return {}
        return json.loads(self._manifest_path.read_text())

    def path(self, parte: str, sha256: str) -> Path:
        return self.root / parte / f"{sha256}.pdf"

    def put(self, listing: ParteListing, pdf: Pdf) -> str:
        manifest = self.manifest()
        previous = manifest.get(listing.parte, {}).get("sha256")
        status = self.NEW if previous is None else self.UNCHANGED if previous == pdf.sha256 else self.CHANGED
        path = self.path(listing.parte, pdf.sha256)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pdf.data)
        entry = manifest.get(listing.parte, {})
        if status != self.UNCHANGED:
            entry["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry.update(titulo=listing.titulo, share_url=listing.share_url, sha256=pdf.sha256)
        manifest[listing.parte] = entry
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return status

    def latest(self, parte: str) -> tuple[ParteListing, Pdf] | None:
        """The newest stored PDF of a Parte, with the listing it was downloaded under."""
        entry = self.manifest().get(parte)
        if entry is None:
            return None
        path = self.path(parte, entry["sha256"])
        data = path.read_bytes()
        pdf = Pdf(data=data, sha256=hashlib.sha256(data).hexdigest())
        if pdf.sha256 != entry["sha256"]:
            raise CorpusError(f"Parte {parte}: {path} does not match its content hash")
        return ParteListing(parte=parte, titulo=entry["titulo"], share_url=entry["share_url"]), pdf


def load_partes(
    partes: list[str],
    store: CorpusStore,
    offline: bool = False,
    on_progress: Callable[[str], None] = lambda _msg: None,
    client: httpx.Client | None = None,
) -> list[SourcedPdf]:
    """Download each Parte into the store; when ANAC can't serve it, fall back to the newest stored PDF.

    `offline` skips ANAC entirely. After a transport error the rest of the run is offline too,
    so a host that is down costs one round of retries, not one per Parte.
    """
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120, connect=10))
    unreachable = "offline run" if offline else None
    listings: dict[str, ParteListing] = {}
    if not offline:
        try:
            listings = {l.parte: l for l in fetch_listings(partes, client)}
        except httpx.HTTPError as e:
            unreachable = f"RAAC listing unreachable ({type(e).__name__}: {e})"
    sourced = []
    for parte in partes:
        reason = unreachable
        if reason is None:
            listing = listings[parte]
            on_progress(strings.PROGRESS_DOWNLOADING.format(parte=parte))
            try:
                pdf = download(listing, client)
            except httpx.HTTPError as e:
                reason = f"download failed ({type(e).__name__}: {e})"
                if isinstance(e, httpx.TransportError):
                    unreachable = reason
            else:
                store.put(listing, pdf)
                sourced.append(SourcedPdf(listing, pdf, from_cache=False))
                continue
        cached = store.latest(parte)
        if cached is None:
            raise CorpusError(f"Parte {parte}: {reason} and no cached PDF in {store.root}")
        listing, pdf = cached
        on_progress(strings.PROGRESS_FROM_CACHE.format(parte=parte, sha256=pdf.sha256[:12]))
        sourced.append(SourcedPdf(listing, pdf, from_cache=True))
    return sourced


def _resolve_folder_file(listing: ParteListing, client: httpx.Client) -> str:
    """WebDAV URL of the file `openfile` names inside a folder share (public WebDAV, token as user)."""
    folder_url = f"{ANAC_HOST}/public.php/webdav" + urllib.parse.quote(listing.folder.rstrip("/") + "/")
    response = _request(
        client,
        "PROPFIND",
        folder_url,
        auth=(listing.token, ""),
        headers={"Depth": "1", "Content-Type": "application/xml"},
        content=_WEBDAV_PROPFIND,
    )
    ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
    for entry in ET.fromstring(response.content).findall("d:response", ns):
        href = entry.findtext("d:href", namespaces=ns)
        if href and entry.findtext(".//oc:fileid", namespaces=ns) == listing.file_id:
            return ANAC_HOST + href
    raise CorpusError(f"Parte {listing.parte}: file {listing.file_id} not found in shared folder {listing.folder!r}")


def _get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    return _request(client, "GET", url, **kwargs)


def _request(client: httpx.Client, method: str, url: str, attempts: int = 6, **kwargs) -> httpx.Response:
    # docs.anac.gob.ar resets connections intermittently; retry transport errors only.
    for attempt in range(attempts):
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(2 ** (attempt + 1), 30))
    raise AssertionError("unreachable")
