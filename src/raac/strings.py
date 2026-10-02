"""User-facing strings. Spanish only."""

NOTICE = (
    "Aviso: esta herramienta no es una fuente oficial y no reemplaza al AIP, "
    "los NOTAM ni a la ANAC."
)
REFUSAL = "No encontré respaldo en la RAAC vigente cargada para responder esta pregunta."
CITATIONS_HEADER = "Citas:"
CITATION = (
    "[{n}] Parte {parte}, Sección {seccion} ({titulo}), {paginas}, "
    "Edición {edicion} Enmienda {enmienda} - {url}"
)
PAGES_SINGLE = "página PDF {pdf} (página impresa {impresa})"
PAGES_RANGE = "páginas PDF {pdf_start}-{pdf_end} (páginas impresas {impresa_start}-{impresa_end})"
PROGRESS_DOWNLOADING = "Descargando Parte {parte}..."
PROGRESS_INDEXING = "Indexando Parte {parte}..."
PROGRESS_SEARCHING = "Buscando en Parte {parte}..."
PROGRESS_READING = "Leyendo Parte {parte}, páginas PDF {pages}"
PROGRESS_ANSWERING = "Redactando la respuesta..."
FETCH_STATUS = {"new": "nueva", "changed": "cambiada", "unchanged": "sin cambios"}
FETCH_LINE = "Parte {parte}: {estado} ({sha256}) - {titulo}"
FETCH_SUMMARY = "{total} Partes descargadas: {new} nuevas, {changed} cambiadas, {unchanged} sin cambios."
