"""User-facing strings. Spanish only."""

NOTICE = (
    "Aviso: esta herramienta no es una fuente oficial y no reemplaza al AIP, "
    "los NOTAM ni a la ANAC."
)
REFUSAL = "No encontré respaldo en la RAAC vigente cargada para responder esta pregunta."
INCOMPLETE = "La RAAC vigente cargada no cubre todo lo que preguntaste. Esto es lo que encontré:"
LIKELY_PARTE = "Lo que falta probablemente lo regula la Parte {parte}: la Sección {seccion} remite a ella."
CITATIONS_HEADER = "Citas:"
CITATION = "[{n}] Parte {parte}, Sección {seccion} ({titulo}), {paginas}, {version} - {url}"
CITATION_NO_VERSION = "[{n}] Parte {parte}, Sección {seccion} ({titulo}), {paginas} - {url}"
VERSION = "Edición {edicion}"
VERSION_ENMIENDA = "Edición {edicion} Enmienda {enmienda}"
VERSION_FECHA = "{version} ({fecha})"
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
PROGRESS_ROUTING = "Eligiendo en qué Partes buscar..."
PROGRESS_ROUTED = "Buscando en: {partes}"


def version(edicion: str | None, enmienda: str | None, fecha: str | None) -> str | None:
    """The cited page's footer as printed in Citations; many Partes print only the Edición."""
    if edicion is None:
        return None
    text = VERSION_ENMIENDA.format(edicion=edicion, enmienda=enmienda) if enmienda else VERSION.format(edicion=edicion)
    return VERSION_FECHA.format(version=text, fecha=fecha) if fecha else text
