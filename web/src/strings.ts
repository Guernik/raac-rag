// Every user-facing string in the UI. Spanish only.

export const strings = {
  appTitle: "RAAC consulta",
  notice: "No es una fuente oficial. No reemplaza al AIP, los NOTAM ni a la ANAC.",
  placeholder: "Escribí tu pregunta",
  send: "Preguntar",
  sending: "Buscando…",
  emptyTitle: "Preguntá sobre las regulaciones de aviación civil argentina.",
  emptyBody: "Cada afirmación de la respuesta cita la Parte y la Sección de la RAAC vigente de donde sale.",
  emptyExample: "Por ejemplo: ¿puedo volar VFR de noche con licencia de piloto privado?",
  refusal: "No encontré respaldo en la RAAC vigente cargada para responder esta pregunta.",
  incomplete: "La RAAC vigente cargada no cubre todo lo que preguntaste. Esto es lo que encontré:",
  likelyParte: (parte: string, seccion: string) =>
    `Lo que falta probablemente lo regula la Parte ${parte}: la Sección ${seccion} remite a ella.`,
  networkError: "No me pude conectar con el servidor. Revisá tu conexión y probá de nuevo.",
  citationTitle: (c: { parte: string; seccion: string }) => `Parte ${c.parte}, Sección ${c.seccion}`,
  definicion: (term: string) => `Definición de «${term}»`,
  pages: (start: number, end: number, printedStart: string | null, printedEnd: string | null) => {
    const pdf = start === end ? `página PDF ${start}` : `páginas PDF ${start}-${end}`;
    if (!printedStart) return pdf;
    const printed = printedStart === printedEnd ? `impresa ${printedStart}` : `impresas ${printedStart}-${printedEnd}`;
    return `${pdf} (${printed})`;
  },
  version: (edicion: string | null, enmienda: string | null, fecha: string | null) => {
    if (!edicion) return null;
    const base = enmienda ? `Edición ${edicion} Enmienda ${enmienda}` : `Edición ${edicion}`;
    return fecha ? `${base} (${fecha})` : base;
  },
  openSource: "Abrir el PDF de la Parte",
  close: "Cerrar",
};
