# Idea: NOTAM Q&A

Out of scope for now. A future product: ask questions over the complete set of NOTAMs currently active in Argentina and get an answer with the NOTAMs themselves as evidence.

Examples:

- "Decime todas las pistas cerradas" -> a list of runway closures, each item citing its NOTAM.
- "¿Qué restricciones hay en SAEZ mañana a la mañana?"
- "¿Hay algún VOR fuera de servicio en la ruta SABE-SAZS?"

Differs from RAAC Q&A in shape:

- The corpus is short-lived and churns constantly (minutes to hours), not slow-moving Enmiendas.
- NOTAMs are semi-structured (Q-line code, location, validity window, text), so many questions are filters and aggregations ("all", "which", "tomorrow"), better served by parsing into structured records and querying than by tree retrieval. The LLM turns the question into a query and explains the results.
- Validity time matters: an answer must state the time it was computed for.

The RAAC Q&A pieces that carry over: Citations as data, grounded-or-silent Answers, the eval harness, and the API that briefing tools would consume.
