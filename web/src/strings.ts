// Every user-facing string in the UI. Spanish only.

export const strings = {
  appTitle: "RAAC consulta",
  beta: "beta privada",
  notice: "No es una fuente oficial. No reemplaza al AIP, los NOTAM ni a la ANAC.",
  themeToDark: "Usar tema oscuro",
  themeToLight: "Usar tema claro",
  question: "Pregunta",
  placeholder: "Escribí tu pregunta",
  send: "Preguntar",
  hint: "Enter envía · Shift+Enter agrega una línea",
  emptyTitle: "Preguntá sobre las regulaciones de aviación civil argentina.",
  emptyBody: "Cada afirmación de la respuesta cita la Parte y la Sección de la RAAC vigente de donde sale.",
  examplesLabel: "Por ejemplo",
  examples: [
    "¿Puedo volar VFR de noche con mi licencia de piloto privado?",
    "Soy PPL y no vuelo hace 4 meses, ¿puedo salir como PIC o tengo que hacer algo antes?",
    "¿Qué se considera noche según la RAAC?",
  ],
  refusalLabel: "Sin respaldo",
  refusal: "No encontré respaldo en la RAAC vigente cargada para responder esta pregunta.",
  incompleteLabel: "Respuesta parcial",
  incomplete: "La RAAC vigente cargada no cubre todo lo que preguntaste. Esto es lo que encontré:",
  likelyParte: (parte: string, seccion: string) =>
    `Lo que falta probablemente lo regula la Parte ${parte}: la Sección ${seccion} remite a ella.`,
  errorLabel: "Sin respuesta",
  networkError: "No me pude conectar con el servidor. Revisá tu conexión y probá de nuevo.",
  retry: "Reintentar",
  elapsed: (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`,
  citedLabel: "Secciones citadas",
  citedEmpty: "Elegí una Sección de la respuesta para ver el texto citado.",
  citationLabel: (c: { parte: string; seccion: string }) => `Parte ${c.parte}, Sección ${c.seccion}`,
  citationKicker: (c: { parte: string; seccion: string }) => `Parte ${c.parte} · Sección ${c.seccion}`,
  definicion: (term: string) => `Definición de «${term}»`,
  pdfPage: "Página PDF",
  printedPage: "Página impresa",
  versionLabel: "Versión",
  range: (start: string | number, end: string | number) => (start === end ? `${start}` : `${start}-${end}`),
  version: (edicion: string | null, enmienda: string | null, fecha: string | null) => {
    if (!edicion) return null;
    const base = enmienda ? `Edición ${edicion} Enmienda ${enmienda}` : `Edición ${edicion}`;
    return fecha ? `${base} (${fecha})` : base;
  },
  openSource: "Abrir el PDF de la Parte",
  close: "Cerrar",

  // Beta gate (#26)
  inviteTitle: "Beta privada",
  inviteBody: "Por ahora RAAC consulta funciona solo con invitación. Ingresá el código que te pasaron.",
  inviteCode: "Código de invitación",
  inviteSubmit: "Entrar",
  inviteInvalid: "Ese código no es válido. Revisalo y probá de nuevo.",
  closedTitle: "Volvé mañana",
  closedBody: "Por hoy se alcanzó el límite de consultas de la beta. Mañana se habilitan de nuevo.",
};
