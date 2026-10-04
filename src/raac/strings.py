"""User-facing strings. Spanish only."""

NOTICE = (
    "Aviso: esta herramienta no es una fuente oficial y no reemplaza al AIP, "
    "los NOTAM ni a la ANAC."
)
REFUSAL = "No encontré respaldo en la RAAC vigente cargada para responder esta pregunta."
INCOMPLETE = "La RAAC vigente cargada no cubre todo lo que preguntaste. Esto es lo que encontré:"
LIKELY_PARTE = "Lo que falta probablemente lo regula la Parte {parte}: la Sección {seccion} remite a ella."
CITATIONS_HEADER = "Citas:"
DEFINICION_TITLE = "Definición de «{term}»"
CITATION = "[{n}] Parte {parte}, Sección {seccion} ({titulo}), {paginas}, {version} - {url}"
CITATION_NO_VERSION = "[{n}] Parte {parte}, Sección {seccion} ({titulo}), {paginas} - {url}"
VERSION = "Edición {edicion}"
VERSION_ENMIENDA = "Edición {edicion} Enmienda {enmienda}"
VERSION_FECHA = "{version} ({fecha})"
PAGES_SINGLE = "página PDF {pdf} (página impresa {impresa})"
PAGES_RANGE = "páginas PDF {pdf_start}-{pdf_end} (páginas impresas {impresa_start}-{impresa_end})"
PROGRESS_DOWNLOADING = "Descargando Parte {parte}..."
PROGRESS_FROM_CACHE = "Usando copia local de Parte {parte} ({sha256})"
PROGRESS_INDEXING = "Indexando Parte {parte}..."
PROGRESS_SEARCHING = "Buscando en Parte {parte}..."
PROGRESS_READING = "Leyendo Parte {parte}, páginas PDF {pages}"
PROGRESS_ANSWERING = "Redactando la respuesta..."
PROGRESS_UPLOADING = "Subiendo Parte {parte} a PageIndex Cloud..."
PROGRESS_CLOUD = "Consultando PageIndex Cloud..."
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
REVIEW_CARD = (
    "\n[{n}/{total}] {id}\n"
    "Pregunta: {pregunta}\n\n"
    "Sección esperada: Parte {parte}, Sección {seccion} ({titulo})\n{texto}\n\n"
    "Respuesta de referencia: {respuesta}\n"
)
REVIEW_PROMPT = "[a]ceptar, [e]ditar, [r]echazar, [s]altar, [q] salir: "
REVIEW_UNKNOWN = "Opción no válida."
REVIEW_EDIT_QUESTION = "Nueva pregunta (Enter la deja igual): "
REVIEW_EDIT_ANSWER = "Nueva respuesta de referencia (Enter la deja igual): "
REVIEW_SUMMARY = "{aceptados} aceptados, {rechazados} rechazados, {saltados} saltados, {pendientes} pendientes."
GENERATE_SUMMARY = "{generados} candidatos nuevos en {path}; {descartados} descartados."
GENERATE_DISCARDED = "Descartado {nota}"
