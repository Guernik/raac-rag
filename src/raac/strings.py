"""User-facing strings. Spanish only."""

NOTICE = (
    "Aviso: esta herramienta no es una fuente oficial y no reemplaza al AIP, "
    "los NOTAM ni a la ANAC."
)
REFUSAL = "No encontré respaldo en la RAAC vigente cargada para responder esta pregunta."
INCOMPLETE = "La RAAC vigente cargada no cubre todo lo que preguntaste. Esto es lo que encontré:"
LIKELY_PARTE = "Lo que falta probablemente lo regula la Parte {parte}: la Sección {seccion} remite a ella."
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
