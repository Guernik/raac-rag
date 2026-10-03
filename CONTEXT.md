# RAAC Q&A

Answers free-text questions about Argentine civil aviation regulations (RAAC), grounded in and citing the official ANAC documents.

## Language

### Regulations

**RAAC**:
Regulaciones Argentinas de Aviación Civil, the body of civil aviation regulations published by ANAC. Made of many **Partes**.
_Avoid_: the regulations, the rules

**Parte**:
One RAAC regulation, published as a single PDF, identified by a code that is usually a number but not always (Parte 61, Licencias; Parte 91, Reglas de vuelo; Parte HL, Helicópteros Livianos). Contains **Capítulos** or **Subpartes**, which contain **Secciones**.
Pilots and the RAAC text itself name a Parte as "RAAC <code>" (RAAC 67, RAAC 91). User-facing text says "RAAC 67", not "Parte 67"; in code and docs the term stays **Parte**.
_Avoid_: part, document, file

**Sección**:
The smallest numbered, citable unit of a **Parte**, usually identified as `<parte>.<n>` (e.g. 61.535). Subdivided into incisos ((a), (1), (i)).
_Avoid_: section, article, rule, chunk

**Remisión**:
A reference in the text of one **Sección** to another **Parte** or **Sección** (e.g. "conforme a la RAAC 67").
_Avoid_: link, cross-ref

**Definición**:
A term defined in Parte 1 and used across other **Partes**.
_Avoid_: glossary entry

**Edición** / **Enmienda**:
The version of a **Parte**, printed in every page footer (e.g. "VI Edición, Enmienda I, mayo 2026"). A new Enmienda replaces the previous text entirely.
_Avoid_: version, revision

**RAAC vigente**:
The **Edición**/**Enmienda** of each **Parte** currently in force, as listed by ANAC. The only text the product answers from.
_Avoid_: current, latest

### Answers

**Answer**:
A plain-Spanish synthesis responding to a user's question, in which every claim carries at least one **Citation**; only **Framing** may go uncited. Not advice.
_Avoid_: response, reply

**Framing**:
The uncited parts of an **Answer** that state no requirement of their own: a lead-in, a connective, or a verdict (sí / no / depende) that the cited sentences support. Applying a rule to the user's own facts is a claim, not Framing.
_Avoid_: summary, intro

**Citation**:
A pointer from an **Answer** sentence to the **Sección** that supports it: **Parte**, **Sección**, **PDF page**, and **Edición**.
_Avoid_: reference, source, footnote

**Conversation**:
The sequence of questions and **Answers** in one browser tab. Not persisted; closing the tab ends it.
_Avoid_: session, chat, thread

**Standalone question**:
A user's latest message rewritten, using the **Conversation** so far, into a question that makes sense on its own. Retrieval runs on it, never on the raw message.
_Avoid_: rewritten query, condensed question

**PDF page**:
The physical page index in the PDF file (1-based). Distinct from the **printed page**, the label printed in the page footer.
_Avoid_: page (unqualified)

## Flagged ambiguities

- "Page" alone is ambiguous: Parte 61's Sección 61.535 is on **PDF page** 67 but **printed page** 10. Always qualify it.

## Example dialogue

> **Dev:** The user asks about night VFR. Do we cite page 10?
> **Domain expert:** Cite Sección 61.535 of Parte 61, VI Edición Enmienda I. Page 10 is the printed page; the link goes to PDF page 67.
> **Dev:** And if Parte 67 isn't in the RAAC vigente we loaded?
> **Domain expert:** Then the Answer says we don't have it and cites only what Parte 61 says about the medical requirement.
