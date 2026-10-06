"""CorpusSource against saved copies of the ANAC RAAC page and the RAAC Vigentes sheet (2026-10-02)."""

import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from raac import corpus
from raac.corpus import CorpusError, CorpusStore, ParteListing, Pdf, parse_sheet, sheet_id_from_page

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_HTML = (FIXTURES / "anac-raac-page.html").read_text(encoding="utf-8")
SHEET_CSV = (FIXTURES / "raac-vigentes-sheet.csv").read_text(encoding="utf-8")
SHEET_ID = "1Xu4sfqfp29hLAHOC2i-FjfwFkKnxQj0uDoXFuT-ILOg"
HEADER = "parte,titulo,btn-Ver,orden\nParte,Título,Documento,orden\n"


def _client(routes: dict[str, bytes], requests: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        body = routes.get(f"{request.method} {request.url}") or routes.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sheet_id_read_from_saved_page():
    assert sheet_id_from_page(PAGE_HTML) == SHEET_ID


def test_sheet_id_read_from_poncho_config():
    html = '<script>var ponchoTableOpciones = {"idSpread": "abc_DEF-123", "hojaNombre": "x"};</script>'
    assert sheet_id_from_page(html) == "abc_DEF-123"


def test_missing_poncho_config_fails_loudly():
    with pytest.raises(CorpusError, match="idSpread"):
        sheet_id_from_page("<html><script>var ponchoTableOpciones = {};</script></html>")


def test_lists_all_57_partes_from_saved_page_and_sheet():
    client = _client(
        {
            corpus.RAAC_PAGE_URL: PAGE_HTML.encode(),
            corpus.SHEET_CSV_URL.format(id=SHEET_ID): SHEET_CSV.encode(),
        }
    )
    listings = corpus.list_partes(client)
    codes = [l.parte for l in listings]
    assert len(listings) == 57
    assert len(set(codes)) == 57
    assert {"1", "61", "67", "91", "HL", "HML", "VLA"} <= set(codes)
    assert "Parte" not in codes  # Spanish header row skipped


def test_rows_normalized():
    by_code = {l.parte: l for l in parse_sheet(SHEET_CSV)}
    # `**` markup and trailing whitespace stripped from titles
    assert by_code["61"].titulo == "Licencias, Certificado de Competencia y Habilitaciones para piloto"
    assert by_code["HL"].titulo == "Helicópteros Livianos"
    # trailing whitespace in the Parte code ("Parte 129 ")
    assert by_code["129"].share_url == "https://docs.anac.gob.ar/index.php/s/3GmN8AaEHBZrTKD"
    # trailing newline in the link (Parte 153)
    assert by_code["153"].share_url == "https://docs.anac.gob.ar/index.php/s/PQQ2NFoHKS4Lypx"
    assert by_code["153"].download_url == "https://docs.anac.gob.ar/index.php/s/PQQ2NFoHKS4Lypx/download"
    # Parte 77 links into a shared folder: kept as published, with the folder and file id
    assert by_code["77"].share_url == (
        "https://docs.anac.gob.ar/index.php/s/9mGAnGA8yo86GYn"
        "?dir=undefined&path=%2FBiblioteca%20Virtual%2FRAAC%2FRAAC%2077&openfile=324083"
    )
    assert (by_code["77"].folder, by_code["77"].file_id) == ("/Biblioteca Virtual/RAAC/RAAC 77", "324083")
    assert [code for code, l in by_code.items() if l.file_id] == ["77"]
    for listing in by_code.values():
        assert "*" not in listing.titulo and listing.titulo == listing.titulo.strip()
        assert "\n" not in listing.share_url


def test_inner_whitespace_in_title_collapsed():
    sheet = HEADER + 'Parte 61,"**Licencias,   Certificado\n de piloto** ",https://docs.anac.gob.ar/index.php/s/abc,1\n'
    assert parse_sheet(sheet)[0].titulo == "Licencias, Certificado de piloto"


@pytest.mark.parametrize(
    "row, error",
    [
        ("Parte HL,**Helicópteros Livianos**,https://example.com/s/abc,1", "unexpected link"),
        ("Parte HL,**Helicópteros Livianos**,http://docs.anac.gob.ar/index.php/s/abc,1", "unexpected link"),
        ("Parte HL,**Helicópteros Livianos**,https://docs.anac.gob.ar/index.php/s/abc/download,1", "unexpected link"),
        ("Parte HL,**Helicópteros Livianos**,https://docs.anac.gob.ar/index.php/s/abc?x=1 y,1", "unexpected link"),
        ("Parte HL,**Helicópteros Livianos**,https://docs.anac.gob.ar/index.php/s/abc?path=%2FRAAC,1", "unexpected link"),
        ("Parte HL,**Helicópteros Livianos**,https://docs.anac.gob.ar/index.php/s/abc?openfile=12,1", "unexpected link"),
        ("Parte HL,**Helicópteros Livianos**,,1", "no link"),
        ("Parte HL,**Helicópteros Livianos**,   ,1", "no link"),
        ("Parte HL,** **,https://docs.anac.gob.ar/index.php/s/abc,1", "no titulo"),
        ("RAAC 61,**Licencias**,https://docs.anac.gob.ar/index.php/s/abc,1", "unexpected Parte"),
        (",**Licencias**,https://docs.anac.gob.ar/index.php/s/abc,1", "unexpected Parte"),
        ("Parte 61,**Licencias**,https://docs.anac.gob.ar/index.php/s/abc", "columns"),
    ],
)
def test_unexpected_rows_fail_loudly(row, error):
    with pytest.raises(CorpusError, match=error):
        parse_sheet(HEADER + row + "\n")


def test_duplicate_parte_fails_loudly():
    row = "Parte 61,**Licencias**,https://docs.anac.gob.ar/index.php/s/abc,1\n"
    with pytest.raises(CorpusError, match="twice"):
        parse_sheet(HEADER + row + row)


def test_changed_header_fails_loudly():
    with pytest.raises(CorpusError, match="header"):
        parse_sheet("parte,titulo,link,orden\nParte 61,**Licencias**,https://docs.anac.gob.ar/index.php/s/abc,1\n")


def test_empty_sheet_fails_loudly():
    with pytest.raises(CorpusError, match="no Partes"):
        parse_sheet(HEADER)


def test_unlisted_parte_fails_loudly():
    with pytest.raises(CorpusError, match="not listed"):
        corpus.find_listing(parse_sheet(SHEET_CSV), "999")


LISTING = ParteListing(parte="HL", titulo="Helicópteros Livianos", share_url="https://docs.anac.gob.ar/index.php/s/abc")


def test_download_hashes_pdf():
    client = _client({LISTING.download_url: b"%PDF-1.7 v1"})
    pdf = corpus.download(LISTING, client)
    assert pdf.data == b"%PDF-1.7 v1"
    assert pdf.sha256 == hashlib.sha256(b"%PDF-1.7 v1").hexdigest()


FOLDER_LISTING = parse_sheet(SHEET_CSV)[[l.parte for l in parse_sheet(SHEET_CSV)].index("77")]
FOLDER_URL = "https://docs.anac.gob.ar/public.php/webdav/Biblioteca%20Virtual/RAAC/RAAC%2077/"
FOLDER_PROPFIND = (FIXTURES / "anac-share-folder-parte-77.xml").read_bytes()


def test_download_resolves_file_in_shared_folder():
    requests: list[httpx.Request] = []
    client = _client(
        {
            f"PROPFIND {FOLDER_URL}": FOLDER_PROPFIND,
            f"GET {FOLDER_URL}RAAC%20PARTE%2077.pdf": b"%PDF-1.7 parte 77",
        },
        requests,
    )
    assert corpus.download(FOLDER_LISTING, client).data == b"%PDF-1.7 parte 77"
    # public WebDAV authenticates with the share token as user
    token_auth = "Basic " + base64.b64encode(b"9mGAnGA8yo86GYn:").decode()
    assert [r.headers["Authorization"] for r in requests] == [token_auth, token_auth]


def test_download_fails_when_file_missing_from_shared_folder():
    listing = ParteListing(
        parte="77", titulo="x", share_url=FOLDER_LISTING.share_url, folder=FOLDER_LISTING.folder, file_id="1"
    )
    client = _client({f"PROPFIND {FOLDER_URL}": FOLDER_PROPFIND})
    with pytest.raises(CorpusError, match="not found in shared folder"):
        corpus.download(listing, client)


def test_download_rejects_non_pdf():
    client = _client({LISTING.download_url: b"<html>login</html>"})
    with pytest.raises(CorpusError, match="not a PDF"):
        corpus.download(LISTING, client)


def _pdf(data: bytes) -> Pdf:
    return Pdf(data=data, sha256=hashlib.sha256(data).hexdigest())


def test_store_detects_new_unchanged_and_changed(tmp_path):
    store = CorpusStore(tmp_path)
    v1, v2 = _pdf(b"%PDF v1"), _pdf(b"%PDF v2")

    assert store.put(LISTING, v1) == CorpusStore.NEW
    assert store.path("HL", v1.sha256).read_bytes() == b"%PDF v1"
    fetched_at = store.manifest()["HL"]["fetched_at"]

    assert CorpusStore(tmp_path).put(LISTING, v1) == CorpusStore.UNCHANGED
    assert store.manifest()["HL"]["fetched_at"] == fetched_at

    assert store.put(LISTING, v2) == CorpusStore.CHANGED
    assert store.manifest()["HL"] == {
        "titulo": "Helicópteros Livianos",
        "share_url": LISTING.share_url,
        "sha256": v2.sha256,
        "fetched_at": store.manifest()["HL"]["fetched_at"],
    }
    # the previous version stays on disk beside the new one
    assert store.path("HL", v1.sha256).exists() and store.path("HL", v2.sha256).exists()


def test_fetch_command_reports_unchanged_on_second_run(tmp_path, monkeypatch, capsys):
    from raac import cli

    monkeypatch.setattr(corpus, "list_partes", lambda client: [LISTING])
    monkeypatch.setattr(corpus, "download", lambda listing, client: _pdf(b"%PDF v1"))
    assert cli.main(["fetch", "--dir", str(tmp_path)]) == 0
    assert "Parte HL: nueva" in capsys.readouterr().out
    assert cli.main(["fetch", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Parte HL: sin cambios" in out
    assert "1 Partes descargadas: 0 nuevas, 0 cambiadas, 1 sin cambios." in out


# load_partes: CLI and evals keep working from the cache when docs.anac.gob.ar is down

SHEET_ROUTES = {corpus.RAAC_PAGE_URL: PAGE_HTML.encode(), corpus.SHEET_CSV_URL.format(id=SHEET_ID): SHEET_CSV.encode()}


def _anac_down(requests: list) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "docs.anac.gob.ar":
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, content=SHEET_ROUTES[str(request.url)])

    return httpx.Client(transport=httpx.MockTransport(handler))


def _cached(store: CorpusStore, parte: str, data: bytes) -> Pdf:
    listing = corpus.find_listing(parse_sheet(SHEET_CSV), parte)
    pdf = _pdf(data)
    store.put(listing, pdf)
    return pdf


def test_falls_back_to_newest_cached_pdf_when_anac_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus.time, "sleep", lambda _s: None)
    store = CorpusStore(tmp_path)
    _cached(store, "61", b"%PDF 61 old")
    pdf61 = _cached(store, "61", b"%PDF 61 new")
    pdf91 = _cached(store, "91", b"%PDF 91")
    requests, progress = [], []

    sourced = corpus.load_partes(["61", "91"], store, on_progress=progress.append, client=_anac_down(requests))

    assert [(s.listing.parte, s.pdf, s.from_cache) for s in sourced] == [("61", pdf61, True), ("91", pdf91, True)]
    assert sourced[0].listing.share_url == store.manifest()["61"]["share_url"]
    assert f"Usando copia local de Parte 61 ({pdf61.sha256[:12]})" in progress
    # a host that is down costs one round of retries, not one per Parte
    assert len([r for r in requests if r.url.host == "docs.anac.gob.ar"]) == 6


def test_anac_down_without_cache_names_the_parte(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus.time, "sleep", lambda _s: None)
    with pytest.raises(CorpusError, match="Parte 61: download failed.*no cached PDF"):
        corpus.load_partes(["61"], CorpusStore(tmp_path), client=_anac_down([]))


def test_offline_makes_no_request(tmp_path):
    store = CorpusStore(tmp_path)
    pdf = _cached(store, "61", b"%PDF 61")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"offline run requested {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    sourced = corpus.load_partes(["61"], store, offline=True, client=client)
    assert [(s.pdf, s.from_cache) for s in sourced] == [(pdf, True)]


def test_downloads_into_the_store_when_anac_is_up(tmp_path):
    listing = corpus.find_listing(parse_sheet(SHEET_CSV), "61")
    client = _client({**SHEET_ROUTES, listing.download_url: b"%PDF 61"})
    store = CorpusStore(tmp_path)
    sourced = corpus.load_partes(["61"], store, client=client)
    assert [(s.pdf, s.from_cache) for s in sourced] == [(_pdf(b"%PDF 61"), False)]
    assert store.latest("61") == (ParteListing("61", listing.titulo, listing.share_url), _pdf(b"%PDF 61"))
