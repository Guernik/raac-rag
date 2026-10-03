# Answers may carry uncited Framing; every claim stays cited

Dropping every uncited sentence made Answers lose their direct verdict ("Sí, tenés que avisarle.") and the lead-ins that hold a list of conditions together, and in one case deleted the line saying the Secciones did not cover the question. PageIndex.ai's presentation (a "Depende de...:" lead-in, then one cited item per condition) reads better on a phone, so an Answer may now include uncited **Framing**: lead-ins, connectives, and a verdict (sí / no / depende) that the cited sentences of the same Answer support. Everything else stays cited: requirements, numbers, dates, deadlines, Sección references, and any application of a rule to the user's own facts ("con 4 meses sin volar estarías dentro") are claims, not Framing.

## Considered Options

- **Every sentence cited, verdict folded into a cited sentence by the prompt.** Safest, but fights the Citations API, which returns prose between cited spans, and still reads worse.
- **Also allow applying the rule to the user's facts.** Most useful, but it is the model's own inference about the pilot's situation, the thing a pilot is most likely to act on.

## Consequences

- Framing is checked deterministically, not trusted: uncited text with a number, date, or Sección reference not present in the Answer's cited text is dropped. An Answer whose only sentences are Framing is a refusal.
- A coverage gap is never dropped as uncited text; it makes the Answer incomplete or a refusal.
- The grounding score counts claims, not sentences: uncited Framing is not a grounding failure, a dropped uncited claim is.
