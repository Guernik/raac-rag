# The downloaded PDF is the version; footers are per-page data

We required every page footer of a Parte to print the same Edición/Enmienda and refused to index a Parte otherwise. In the full RAAC vigente, Partes 26, 135 and 139 fail that check and 77 nearly did ("Enmienda 1" beside "Enmienda I"). The footers are page-level and unreliable: unchanged pages keep an older revision by design (each Parte's "Lista de verificación de páginas" gives a revision date per division), and some footers are plainly wrong (139 PDF page 6 prints "4º Edición 07 enero 2019" between "1º Edición 07 enero 2019" pages; 135's Subparte K prints "5º Edición 21 enero 2025" in a 4º Edición whose verification list dates it 08/06/2022). Cover pages print an Edición for only some Partes. Yet the PDF ANAC publishes is the enacted text as a whole.

So the version of a Parte is the PDF itself: its content hash, with the share URL and fetch date. Each page keeps the Edición, Enmienda and date its own footer prints, and a Citation shows those of the cited page, which is what the reader sees when the PDF opens there. A Parte-level Edición/Enmienda (the most common footer value) is kept for logs only and never gates indexing.

## Considered Options

- **Majority vote, outliers logged.** Indexes everything, but a Parte-level Edición still lands in Citations whose cited page may print another one.
- **Edición from the cover or the Registro de enmiendas.** Covers print it for 15 of 57 Partes; the Registro is a hand-kept table in many layouts.
- **Keep failing on disagreement.** Leaves three enacted Partes unanswerable until ANAC fixes footers it may never fix.

## Consequences

- Promotion checks are: the PDF parses, page count is sane, and the Parte's eval cases pass. A PDF with no readable footer at all still fails to parse.
- Change detection and the index version in logs use the content hash only.
- A Citation can show a different Edición than another Citation of the same Parte. That is what the pages say.
